"""Scheduled sends: approve now, deliver at send_at. The dangerous cases are
double-fire (two ticks racing), cancel racing the scheduler, and rate-limited
batches — all resolved by the atomic scheduled→sending claim + release."""

import time

from app import db, policy, scheduler

from .conftest import BOB, admin_headers, bearer


def _make_due(draft_id):
    with db.connect() as conn:
        conn.execute("UPDATE drafts SET send_at = ? WHERE id = ?",
                     (int(time.time()) - 10, draft_id))


def _status(draft_id):
    with db.connect() as conn:
        return conn.execute("SELECT status FROM drafts WHERE id = ?",
                            (draft_id,)).fetchone()["status"]


def _schedule_direct(client, key, text="later"):
    r = client.post("/v1/send", json={"to": BOB, "text": text,
                                      "send_at": int(time.time()) + 3600},
                    headers=bearer(key))
    assert r.status_code == 202 and r.json()["status"] == "scheduled"
    return r.json()["draft_id"]


# ------------------------------------------------------------ creation paths

def test_direct_key_schedules_instead_of_sending(client, env, make_key, fake_sidecar):
    key = make_key(name="sender", role="read-send")
    draft_id = _schedule_direct(client, key)
    assert fake_sidecar["send"] == []                     # nothing delivered yet
    assert _status(draft_id) == "scheduled"
    rows = client.get("/v1/admin/drafts?status=scheduled", headers=admin_headers()).json()
    assert [r["id"] for r in rows] == [draft_id]


def test_draft_approval_parks_as_scheduled(client, env, make_key, fake_sidecar):
    key = make_key(name="drafter", role="read-draft")
    r = client.post("/v1/drafts", json={"to": BOB, "text": "tomorrow",
                                        "delay_seconds": 3600}, headers=bearer(key))
    assert r.status_code == 201 and r.json()["send_at"]
    draft_id = r.json()["id"]
    ok = client.post(f"/v1/admin/drafts/{draft_id}/approve", headers=admin_headers())
    assert ok.json()["status"] == "scheduled"
    assert fake_sidecar["send"] == []                     # approved ≠ sent yet
    _make_due(draft_id)
    scheduler._tick()
    assert fake_sidecar["send"] == [(BOB, "tomorrow")]
    assert _status(draft_id) == "sent"


def test_validation(client, env, make_key):
    key = make_key(name="sender", role="read-send")
    past = {"to": BOB, "text": "x", "send_at": int(time.time()) - 100}
    assert client.post("/v1/send", json=past, headers=bearer(key)).status_code == 400
    far = {"to": BOB, "text": "x", "send_at": int(time.time()) + 40 * 86400}
    assert client.post("/v1/send", json=far, headers=bearer(key)).status_code == 400
    both = {"to": BOB, "text": "x", "send_at": int(time.time()) + 3600, "delay_seconds": 60}
    assert client.post("/v1/send", json=both, headers=bearer(key)).status_code == 400


# ------------------------------------------------------------ firing

def test_scheduler_fires_due_send_with_scheduler_actor(client, env, make_key, fake_sidecar):
    key = make_key(name="sender", role="read-send")
    draft_id = _schedule_direct(client, key, "on time")
    scheduler._tick()                                      # not due yet
    assert fake_sidecar["send"] == []
    _make_due(draft_id)
    scheduler._tick()
    assert fake_sidecar["send"] == [(BOB, "on time")]
    with db.connect() as c:
        row = c.execute("SELECT actor FROM audit_log WHERE action='send.sent'").fetchone()
    assert row["actor"] == "scheduler"


def test_double_tick_sends_exactly_once(client, env, make_key, fake_sidecar):
    key = make_key(name="sender", role="read-send")
    draft_id = _schedule_direct(client, key, "once")
    _make_due(draft_id)
    scheduler._tick()
    scheduler._tick()
    assert len(fake_sidecar["send"]) == 1


def test_cancel_beats_scheduler(client, env, make_key, fake_sidecar):
    key = make_key(name="sender", role="read-send")
    draft_id = _schedule_direct(client, key)
    r = client.delete(f"/v1/drafts/{draft_id}", headers=bearer(key))   # agent cancel
    assert r.json()["status"] == "canceled"
    _make_due(draft_id)
    scheduler._tick()
    assert fake_sidecar["send"] == [] and _status(draft_id) == "canceled"


def test_rate_limited_batch_drains_over_ticks(client, env, make_key, fake_sidecar, monkeypatch):
    key = make_key(name="bulk", role="read-send", rate=2)
    ids = [_schedule_direct(client, key, f"m{i}") for i in range(3)]
    for i in ids:
        _make_due(i)
    scheduler._tick()
    assert len(fake_sidecar["send"]) == 2                  # per-minute budget
    statuses = sorted(_status(i) for i in ids)
    assert statuses == ["scheduled", "sent", "sent"]        # 3rd released, not failed
    # the next minute (fresh limiter window) drains the remainder
    monkeypatch.setattr(policy, "rate_limiter", policy.RateLimiter())
    scheduler._tick()
    assert len(fake_sidecar["send"]) == 3
    assert all(_status(i) == "sent" for i in ids)


def test_sidecar_503_releases_back_to_scheduled(client, env, make_key, fake_sidecar):
    key = make_key(name="sender", role="read-send")
    draft_id = _schedule_direct(client, key)
    _make_due(draft_id)
    fake_sidecar["make_send_fail"](503)
    scheduler._tick()
    assert _status(draft_id) == "scheduled"                # retryable, not failed
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='send.deferred'"
                      " AND actor='scheduler'").fetchone()[0]
    assert n == 1


# ------------------------------------------------------------ cancellation edges

def test_admin_cancel_only_from_scheduled(client, env, make_key, fake_sidecar):
    key = make_key(name="drafter", role="read-draft")
    pending = client.post("/v1/drafts", json={"to": BOB, "text": "x"},
                          headers=bearer(key)).json()["id"]
    r = client.post(f"/v1/admin/drafts/{pending}/cancel", headers=admin_headers())
    assert r.status_code == 409                            # pending ≠ scheduled

    sender = make_key(name="sender", role="read-send")
    sched = _schedule_direct(client, sender)
    r = client.post(f"/v1/admin/drafts/{sched}/cancel", headers=admin_headers())
    assert r.json()["status"] == "canceled"


def test_sent_draft_cannot_be_canceled(client, env, make_key, fake_sidecar):
    key = make_key(name="sender", role="read-send")
    draft_id = _schedule_direct(client, key)
    _make_due(draft_id)
    scheduler._tick()
    assert _status(draft_id) == "sent"
    assert client.delete(f"/v1/drafts/{draft_id}", headers=bearer(key)).status_code == 404


def test_telegram_card_shows_schedule(client, env, make_key, fake_telegram):
    key = make_key(name="drafter", role="read-draft")
    client.post("/v1/drafts", json={"to": BOB, "text": "later", "delay_seconds": 3600},
                headers=bearer(key))
    assert "Scheduled for" in fake_telegram["sent"][0]["text"]
