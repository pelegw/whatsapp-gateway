"""Telegram live-approval channel: notifications, callback dispatch, linking."""

import asyncio

from app import db, grants
from app.notify import telegram as tg

from .conftest import BOB, admin_headers, bearer


def _cb(update_id, data, chat_id=4242, message_id=10):
    return {"update_id": update_id,
            "callback_query": {"id": f"cb{update_id}", "data": data,
                               "message": {"message_id": message_id, "chat": {"id": chat_id}}}}


# ------------------------------------------------------------ notifications

def test_draft_creation_notifies_telegram(client, make_key, fake_telegram):
    key = make_key(name="planner", role="read-draft")
    r = client.post("/v1/send", json={"to": BOB, "text": "hello"}, headers=bearer(key))
    assert r.status_code == 202
    assert len(fake_telegram["sent"]) == 1
    card = fake_telegram["sent"][0]
    assert BOB in card["text"] and "hello" in card["text"]
    assert card["keyboard"]["inline_keyboard"][0][0]["callback_data"].startswith("d:a:")


def test_disabled_telegram_sends_nothing(client, make_key, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    from app.config import get_settings
    get_settings.cache_clear()
    db.set_config("telegram_enabled", "0")          # token present but not enabled
    sent = []
    monkeypatch.setattr("app.notify.telegram._api_send_message",
                        lambda text, keyboard=None: sent.append(text))
    key = make_key(name="p2", role="read-draft")
    client.post("/v1/drafts", json={"to": BOB, "text": "x"}, headers=bearer(key))
    assert sent == []


def test_notify_failure_is_non_fatal(client, make_key, monkeypatch, fake_telegram):
    def boom(*_a, **_k):
        raise tg.TelegramError(502, "down")
    monkeypatch.setattr("app.notify.telegram._api_send_message", boom)
    key = make_key(name="p3", role="read-draft")
    r = client.post("/v1/drafts", json={"to": BOB, "text": "x"}, headers=bearer(key))
    assert r.status_code == 201                      # draft still created
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='notify.failed'").fetchone()[0]
    assert n == 1


# ------------------------------------------------------------ callback dispatch

def test_callback_approves_draft(client, make_key, fake_sidecar, fake_telegram):
    key = make_key(name="planner", role="read-draft")
    draft_id = client.post("/v1/send", json={"to": BOB, "text": "ok?"},
                           headers=bearer(key)).json()["draft_id"]
    tg._handle_update(_cb(1, f"d:a:{draft_id}"))
    assert fake_sidecar["send"] == [(BOB, "ok?")]     # delivered via the tap
    assert client.get(f"/v1/drafts/{draft_id}", headers=bearer(key)).json()["status"] == "sent"
    assert fake_telegram["answered"] and fake_telegram["edited"]


def test_callback_approves_grant(client, make_key, fake_telegram):
    key = make_key(name="ro", role="read-only")
    gid = client.post("/v1/permissions/request",
                      json={"kind": "send_recipient", "contact": BOB},
                      headers=bearer(key)).json()["id"]
    tg._handle_update(_cb(2, f"g:a:{gid}"))
    assert client.get(f"/v1/permissions/{gid}", headers=bearer(key)).json()["status"] == "approved"


def test_callback_from_wrong_chat_is_rejected(client, make_key, fake_sidecar, fake_telegram):
    key = make_key(name="planner", role="read-draft")
    draft_id = client.post("/v1/send", json={"to": BOB, "text": "no"},
                           headers=bearer(key)).json()["draft_id"]
    tg._handle_update(_cb(3, f"d:a:{draft_id}", chat_id=9999))   # not the linked chat
    assert fake_sidecar["send"] == []                            # nothing sent
    assert client.get(f"/v1/drafts/{draft_id}", headers=bearer(key)).json()["status"] == "pending"
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='telegram.rejected_chat'"
                         ).fetchone()[0] == 1


def test_malformed_callback_ignored(fake_telegram):
    tg._handle_update(_cb(4, "garbage-no-colons"))               # must not raise
    assert fake_telegram["answered"]


def test_claim_conflict_no_double_send(client, make_key, fake_sidecar, fake_telegram):
    key = make_key(name="planner", role="read-draft")
    draft_id = client.post("/v1/send", json={"to": BOB, "text": "once"},
                           headers=bearer(key)).json()["draft_id"]
    # approve on the web console first
    client.post(f"/v1/admin/drafts/{draft_id}/approve", headers=admin_headers())
    assert len(fake_sidecar["send"]) == 1
    # then the Telegram tap arrives — must be a no-op, not a second send
    tg._handle_update(_cb(5, f"d:a:{draft_id}"))
    assert len(fake_sidecar["send"]) == 1
    assert any("already handled" in e["text"] for e in fake_telegram["answered"])


# ------------------------------------------------------------ poll loop + linking

def test_poll_loop_consumes_a_batch(client, make_key, fake_sidecar, fake_telegram):
    key = make_key(name="planner", role="read-draft")
    draft_id = client.post("/v1/send", json={"to": BOB, "text": "loop"},
                           headers=bearer(key)).json()["draft_id"]
    fake_telegram["inject"](_cb(7, f"d:a:{draft_id}"))

    async def run_once():
        task = asyncio.create_task(tg.poll_loop())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if fake_sidecar["send"]:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_once())
    assert fake_sidecar["send"] == [(BOB, "loop")]


def test_status_and_linking(env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    from app.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setattr("app.notify.telegram._get_me", lambda: "wagw_bot")
    monkeypatch.setattr("app.notify.telegram._api_send_message", lambda *a, **k: {"message_id": 1})

    st = tg.status()
    assert st["token_present"] and not st["chat_linked"] and st["enabled"] is False

    tg.start_linking()
    # an inbound message from the user links this chat
    tg._handle_update({"update_id": 1, "message": {"message_id": 9, "chat": {"id": 7777}}})
    assert db.get_config("telegram_chat_id") == "7777"
    assert tg.status()["chat_linked"] is True

    tg.set_enabled(True)
    assert db.get_config("telegram_enabled") == "1"
    tg.unlink()
    assert not tg.status()["chat_linked"] and tg.status()["enabled"] is False
