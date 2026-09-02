"""Telegram live-approval channel: notifications, callback dispatch, linking."""

import asyncio
import contextlib

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

def test_poll_loop_consumes_a_batch(client, make_key, fake_sidecar, fake_telegram, monkeypatch):
    key = make_key(name="planner", role="read-draft")
    draft_id = client.post("/v1/send", json={"to": BOB, "text": "loop"},
                           headers=bearer(key)).json()["draft_id"]
    # First poll returns the tap batch; the next stops the loop cleanly (so no
    # worker thread lingers into the following test).
    calls = {"n": 0}
    def get_updates(offset, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return [_cb(7, f"d:a:{draft_id}")]
        raise asyncio.CancelledError()
    monkeypatch.setattr("app.notify.telegram._get_updates", get_updates)

    async def run():
        with contextlib.suppress(asyncio.CancelledError):
            await tg.poll_loop()

    asyncio.run(run())
    assert fake_sidecar["send"] == [(BOB, "loop")]


def _msg(uid, chat_id, text="", chat_type="private", user_id=333):
    m = {"message_id": uid, "chat": {"id": chat_id, "type": chat_type}, "from": {"id": user_id}}
    if text:
        m["text"] = text
    return {"update_id": uid, "message": m}


def test_secure_linking_requires_code_private_chat(env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    from app.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setattr("app.notify.telegram._get_me", lambda: "wagw_bot")
    monkeypatch.setattr("app.notify.telegram._api_send_message", lambda *a, **k: {"message_id": 1})

    st = tg.status()
    assert st["token_present"] and not st["chat_linked"] and st["enabled"] is False

    code = tg.start_linking()["code"]
    # a stranger with NO code does not link
    tg._handle_update(_msg(1, 9999))
    assert not tg.status()["chat_linked"]
    # the code from a GROUP chat does not link
    tg._handle_update(_msg(2, 7000, text=f"/start {code}", chat_type="group"))
    assert not tg.status()["chat_linked"]
    # correct code from a private chat links and binds the operator user id
    tg._handle_update(_msg(3, 7777, text=f"/start {code}", user_id=333))
    assert db.get_config("telegram_chat_id") == "7777"
    assert db.get_config("telegram_user_id") == "333"

    tg.set_enabled(True)
    tg.unlink()
    assert not tg.status()["chat_linked"] and db.get_config("telegram_user_id") == ""


def test_expired_link_code_does_not_link(env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    from app.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setattr("app.notify.telegram._get_me", lambda: "wagw_bot")
    code = tg.start_linking()["code"]
    monkeypatch.setattr("app.notify.telegram._link_expires", 0.0)   # already expired
    tg._handle_update(_msg(1, 7777, text=f"/start {code}"))
    assert not tg.status()["chat_linked"]


def test_callback_rejected_when_disabled(client, make_key, fake_sidecar, fake_telegram):
    key = make_key(name="planner", role="read-draft")
    draft_id = client.post("/v1/send", json={"to": BOB, "text": "x"},
                           headers=bearer(key)).json()["draft_id"]
    db.set_config("telegram_enabled", "0")                          # kill switch
    tg._handle_update(_cb(1, f"d:a:{draft_id}"))
    assert fake_sidecar["send"] == []
    assert client.get(f"/v1/drafts/{draft_id}", headers=bearer(key)).json()["status"] == "pending"


def test_callback_rejected_for_wrong_user(client, make_key, fake_sidecar, fake_telegram):
    db.set_config("telegram_user_id", "333")                        # only user 333 may approve
    key = make_key(name="planner", role="read-draft")
    draft_id = client.post("/v1/send", json={"to": BOB, "text": "x"},
                           headers=bearer(key)).json()["draft_id"]
    cb = _cb(1, f"d:a:{draft_id}")
    cb["callback_query"]["from"] = {"id": 999}                       # a different user taps
    tg._handle_update(cb)
    assert fake_sidecar["send"] == []
    assert client.get(f"/v1/drafts/{draft_id}", headers=bearer(key)).json()["status"] == "pending"


def test_grant_card_shows_duration_and_breadth(env):
    now = 1700000000
    recip = {"kind": "send_recipient", "to_jid": BOB, "created_at": now,
             "expires_at": now + 2 * 3600, "reason": "r", "key_name": "k"}
    assert "for 2h" in tg._grant_text(recip) and BOB in tg._grant_text(recip)
    window = {"kind": "send_window", "to_jid": None, "created_at": now,
              "expires_at": now + 5 * 3600, "reason": "", "key_name": "k"}
    assert "ANYONE" in tg._grant_text(window) and "for 5h" in tg._grant_text(window)
    forever = {"kind": "send_recipient", "to_jid": BOB, "created_at": now,
               "expires_at": None, "reason": "", "key_name": "k"}
    assert "always" in tg._grant_text(forever).lower()
