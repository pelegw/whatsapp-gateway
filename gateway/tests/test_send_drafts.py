"""The heart of the gateway: send routing, drafts, and rate limits over REST."""

import time

from .conftest import ALICE, BOB, bearer

SEND_SCOPES = ["send:direct", "send:draft", "drafts:read"]


def test_allowlisted_send_delivers(client, make_key, fake_sidecar):
    key = make_key(scopes=SEND_SCOPES, allowlist=[ALICE])
    r = client.post("/v1/send", json={"to": "+972501111111", "text": "hi"},
                    headers=bearer(key))
    assert r.status_code == 200
    assert r.json()["status"] == "sent"
    assert fake_sidecar["send"] == [(ALICE, "hi")]


def test_non_allowlisted_send_becomes_draft(client, make_key, fake_sidecar):
    key = make_key(scopes=SEND_SCOPES, allowlist=[ALICE])
    r = client.post("/v1/send", json={"to": BOB, "text": "hello bob"},
                    headers=bearer(key))
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending_approval"
    assert fake_sidecar["send"] == []  # nothing was actually sent
    r = client.get(f"/v1/drafts/{body['draft_id']}", headers=bearer(key))
    assert r.json()["status"] == "pending"


def test_send_without_scopes_is_403(client, make_key, fake_sidecar):
    key = make_key(scopes=["read:chats"])
    r = client.post("/v1/send", json={"to": BOB, "text": "x"}, headers=bearer(key))
    assert r.status_code == 403
    assert fake_sidecar["send"] == []


def test_direct_only_key_cannot_reach_off_list(client, make_key, fake_sidecar):
    key = make_key(scopes=["send:direct"], allowlist=[ALICE])
    r = client.post("/v1/send", json={"to": BOB, "text": "x"}, headers=bearer(key))
    assert r.status_code == 403


def test_per_key_rate_limit(client, make_key, fake_sidecar):
    key = make_key(scopes=SEND_SCOPES, allowlist=[ALICE], rate=2)
    for _ in range(2):
        assert client.post("/v1/send", json={"to": ALICE, "text": "x"},
                           headers=bearer(key)).status_code == 200
    r = client.post("/v1/send", json={"to": ALICE, "text": "x"}, headers=bearer(key))
    assert r.status_code == 429


def test_unreachable_sidecar_is_503(client, make_key):
    # No fake_sidecar: the real httpx client tries SIDECAR_URL (sidecar.invalid)
    # and the ConnectError must surface as a clean 503, never an unhandled 500.
    key = make_key(scopes=SEND_SCOPES, allowlist=[ALICE])
    r = client.post("/v1/send", json={"to": ALICE, "text": "x"}, headers=bearer(key))
    assert r.status_code == 503


def test_sidecar_failure_reports_and_audits(client, make_key, fake_sidecar):
    key = make_key(scopes=SEND_SCOPES, allowlist=[ALICE])
    fake_sidecar["make_send_fail"]()
    r = client.post("/v1/send", json={"to": ALICE, "text": "x"}, headers=bearer(key))
    assert r.status_code == 503  # sidecar said "not logged in"
    from app import db
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='send.failed'").fetchone()[0] == 1


def test_draft_crud_lifecycle(client, make_key):
    key = make_key(scopes=SEND_SCOPES)
    r = client.post("/v1/drafts",
                    json={"to": BOB, "text": "draft body", "note": "checking in"},
                    headers=bearer(key))
    assert r.status_code == 201
    draft_id = r.json()["id"]

    listed = client.get("/v1/drafts", headers=bearer(key)).json()
    assert [d["id"] for d in listed] == [draft_id]

    assert client.delete(f"/v1/drafts/{draft_id}", headers=bearer(key)).json()["status"] == "canceled"
    assert client.get(f"/v1/drafts/{draft_id}", headers=bearer(key)).json()["status"] == "canceled"
    # Cancel is idempotent-hostile by design: a decided draft can't be re-canceled.
    assert client.delete(f"/v1/drafts/{draft_id}", headers=bearer(key)).status_code == 404


def test_drafts_are_isolated_per_key(client, make_key):
    key_a = make_key(name="a", scopes=SEND_SCOPES)
    key_b = make_key(name="b", scopes=SEND_SCOPES)
    draft_id = client.post("/v1/drafts", json={"to": BOB, "text": "secret"},
                           headers=bearer(key_a)).json()["id"]
    assert client.get(f"/v1/drafts/{draft_id}", headers=bearer(key_b)).status_code == 404
    assert client.get("/v1/drafts", headers=bearer(key_b)).json() == []


def test_dangling_sending_claim_is_failed_by_sweep(client, make_key):
    # If the process died mid-approval a draft can be stuck in 'sending'. The
    # >5min sweep must resolve it to 'failed' so it can't dangle forever.
    key = make_key(scopes=SEND_SCOPES)
    draft_id = client.post("/v1/drafts", json={"to": BOB, "text": "mid-send"},
                           headers=bearer(key)).json()["id"]
    from app import db
    with db.connect() as conn:
        conn.execute("UPDATE drafts SET status='sending', decided_at=? WHERE id=?",
                     (int(time.time()) - 400, draft_id))
    # Any draft read triggers the sweep.
    assert client.get(f"/v1/drafts/{draft_id}", headers=bearer(key)).json()["status"] == "failed"


def test_stale_pending_drafts_expire(client, make_key):
    key = make_key(scopes=SEND_SCOPES)
    draft_id = client.post("/v1/drafts", json={"to": BOB, "text": "old"},
                           headers=bearer(key)).json()["id"]
    from app import db
    with db.connect() as conn:  # age it past the TTL
        conn.execute("UPDATE drafts SET expires_at = ? WHERE id = ?",
                     (int(time.time()) - 1, draft_id))
    assert client.get(f"/v1/drafts/{draft_id}", headers=bearer(key)).json()["status"] == "expired"
