"""Events feed (/v1/events): cursor bootstrap, delivery, long-poll, and the
guards — history-sync replay freshness, privacy filtering, audit quietness."""

import os
import sqlite3
import threading
import time

from app import db

from .conftest import ALICE, BOB, admin_headers, bearer


def _insert_message(chat_jid, msg_id, text, ts=None):
    """Append a message the way the sidecar would (a fresh live row)."""
    conn = sqlite3.connect(os.environ["MESSAGES_DB"])
    conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, 0, 'text', ?, NULL)",
                 (chat_jid, msg_id, chat_jid, ts or int(time.time()), text))
    conn.commit()
    conn.close()


def _bootstrap(client, key):
    r = client.get("/v1/events", headers=bearer(key))
    assert r.status_code == 200
    body = r.json()
    assert body["events"] == []          # never a backlog dump
    return body["cursor"]


def test_bootstrap_returns_top_not_backlog(client, archive, make_key):
    key = make_key(name="reader", role="read-only")
    cursor = _bootstrap(client, key)
    assert cursor == 4                   # archive seeds exactly 4 messages
    # nothing new yet → empty, cursor stays put
    body = client.get(f"/v1/events?cursor={cursor}", headers=bearer(key)).json()
    assert body == {"cursor": cursor, "events": []}


def test_new_message_is_delivered_and_cursor_advances(client, archive, make_key):
    key = make_key(name="reader", role="read-only")
    cursor = _bootstrap(client, key)
    _insert_message(BOB, "NEW1", "fresh news")
    body = client.get(f"/v1/events?cursor={cursor}", headers=bearer(key)).json()
    assert [e["text"] for e in body["events"]] == ["fresh news"]
    assert body["events"][0]["chat_jid"] == BOB
    assert body["cursor"] > cursor
    # delivered once: the new cursor yields nothing
    again = client.get(f"/v1/events?cursor={body['cursor']}", headers=bearer(key)).json()
    assert again["events"] == []


def test_history_sync_replay_is_not_an_event(client, archive, make_key):
    """An old-ts row with a NEW rowid (reconnect history replay) must not be
    delivered — but the cursor must still advance past it (no rescan loop)."""
    key = make_key(name="reader", role="read-only")
    cursor = _bootstrap(client, key)
    _insert_message(ALICE, "OLD1", "ancient replay", ts=int(time.time()) - 3600)
    body = client.get(f"/v1/events?cursor={cursor}", headers=bearer(key)).json()
    assert body["events"] == []
    assert body["cursor"] > cursor       # jumped past the stale row
    # a fresh message after the replay still comes through
    _insert_message(BOB, "NEW2", "current")
    body = client.get(f"/v1/events?cursor={body['cursor']}", headers=bearer(key)).json()
    assert [e["text"] for e in body["events"]] == ["current"]


def test_private_chat_never_appears_in_events(client, archive, make_key):
    key = make_key(name="reader", role="read-only")
    cursor = _bootstrap(client, key)
    client.post("/v1/admin/privacy/chats", json={"jid": ALICE},
                headers=admin_headers())
    _insert_message(ALICE, "SECRET1", "private ping")
    _insert_message(BOB, "PUB1", "public ping")
    body = client.get(f"/v1/events?cursor={cursor}", headers=bearer(key)).json()
    assert [e["text"] for e in body["events"]] == ["public ping"]


def test_empty_polls_write_no_audit(client, archive, make_key):
    key = make_key(name="reader", role="read-only")
    cursor = _bootstrap(client, key)
    for _ in range(5):
        client.get(f"/v1/events?cursor={cursor}", headers=bearer(key))
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='read.events'").fetchone()[0]
    assert n == 0
    _insert_message(BOB, "NEW3", "x")
    client.get(f"/v1/events?cursor={cursor}", headers=bearer(key))
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='read.events'").fetchone()[0]
    assert n == 1                        # only the delivering poll is audited


def test_wait_zero_returns_immediately(client, archive, make_key):
    key = make_key(name="reader", role="read-only")
    cursor = _bootstrap(client, key)
    t0 = time.monotonic()
    body = client.get(f"/v1/events?cursor={cursor}&wait=0", headers=bearer(key)).json()
    assert body["events"] == [] and time.monotonic() - t0 < 0.5


def test_long_poll_returns_early_when_message_lands(client, archive, make_key, monkeypatch):
    monkeypatch.setenv("EVENTS_POLL_INTERVAL_SECONDS", "0.05")
    from app.config import get_settings
    get_settings.cache_clear()
    key = make_key(name="reader", role="read-only")
    cursor = _bootstrap(client, key)
    t = threading.Timer(0.3, _insert_message, args=(BOB, "LATE1", "arrived"))
    t.start()
    t0 = time.monotonic()
    body = client.get(f"/v1/events?cursor={cursor}&wait=5", headers=bearer(key)).json()
    t.join()
    elapsed = time.monotonic() - t0
    assert [e["text"] for e in body["events"]] == ["arrived"]
    assert elapsed < 3                  # returned on arrival, not at the deadline


def test_events_require_read_scope(client, archive, make_key):
    # every role includes read:messages today; a scopes-crafted key without it must 403
    key = make_key(name="norad", scopes=["read:chats"])
    assert client.get("/v1/events", headers=bearer(key)).status_code == 403
