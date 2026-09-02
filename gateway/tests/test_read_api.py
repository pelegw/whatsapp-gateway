"""Read endpoints against a seeded archive (and an absent one)."""

from .conftest import ALICE, BOB, GROUP, bearer


def test_list_chats_ordered_by_recency(client, archive, make_key):
    key = make_key()
    r = client.get("/v1/chats", headers=bearer(key))
    assert r.status_code == 200
    jids = [c["jid"] for c in r.json()]
    assert jids == [BOB, GROUP, ALICE]  # last_message_ts DESC


def test_list_chats_filters_by_name(client, archive, make_key):
    key = make_key()
    r = client.get("/v1/chats", params={"q": "fam"}, headers=bearer(key))
    assert [c["jid"] for c in r.json()] == [GROUP]


def test_chat_messages_with_cursor(client, archive, make_key):
    key = make_key()
    r = client.get(f"/v1/chats/{ALICE}/messages", headers=bearer(key))
    assert [m["id"] for m in r.json()] == ["A2", "A1"]  # newest first
    r = client.get(f"/v1/chats/{ALICE}/messages", params={"before": 1000},
                   headers=bearer(key))
    assert [m["id"] for m in r.json()] == ["A1"]


def test_media_flag_without_media_bytes(client, archive, make_key):
    key = make_key()
    r = client.get(f"/v1/chats/{BOB}/messages", headers=bearer(key))
    msg = r.json()[0]
    assert msg["has_media"] == 1
    assert "media_ref" not in msg  # raw decryption keys never leave the gateway


def test_search_across_and_within_chats(client, archive, make_key):
    key = make_key()
    r = client.get("/v1/messages/search", params={"q": "dessert"}, headers=bearer(key))
    assert [m["id"] for m in r.json()] == ["G1"]
    r = client.get("/v1/messages/search", params={"q": "o", "chat_jid": ALICE},
                   headers=bearer(key))
    assert all(m["chat_jid"] == ALICE for m in r.json())


def test_contacts_search(client, archive, make_key):
    key = make_key()
    r = client.get("/v1/contacts", params={"q": "cohen"}, headers=bearer(key))
    assert [c["jid"] for c in r.json()] == [ALICE]


def test_unknown_chat_is_404(client, archive, make_key):
    key = make_key()
    r = client.get("/v1/chats/999@s.whatsapp.net", headers=bearer(key))
    assert r.status_code == 404


def test_empty_when_archive_missing(client, make_key):
    # Sidecar hasn't paired yet -> no messages.db. Reads degrade to empty, not 500.
    key = make_key()
    assert client.get("/v1/chats", headers=bearer(key)).json() == []
    assert client.get("/v1/contacts", headers=bearer(key)).json() == []


def test_negative_limit_cannot_dump_tables(client, archive, make_key):
    # SQLite treats LIMIT -1 as unlimited; the clamp must stop that.
    key = make_key()
    assert len(client.get("/v1/chats", params={"limit": -1},
                          headers=bearer(key)).json()) == 1
    assert len(client.get(f"/v1/chats/{ALICE}/messages", params={"limit": -5},
                          headers=bearer(key)).json()) == 1


def test_same_second_pagination_with_before_id(client, archive, make_key):
    # Two messages in the same second: a plain ts cursor would skip one.
    import os
    import sqlite3
    conn = sqlite3.connect(os.environ["MESSAGES_DB"])
    conn.execute("INSERT INTO messages VALUES (?, 'A3', ?, 1000, 0, 'text', 'same second', NULL)",
                 (ALICE, ALICE))
    conn.commit()
    conn.close()
    key = make_key()
    page1 = client.get(f"/v1/chats/{ALICE}/messages", params={"limit": 1},
                       headers=bearer(key)).json()
    top = page1[0]
    page2 = client.get(f"/v1/chats/{ALICE}/messages",
                       params={"limit": 10, "before": top["ts"], "before_id": top["id"]},
                       headers=bearer(key)).json()
    ids = {m["id"] for m in page1} | {m["id"] for m in page2}
    assert ids == {"A1", "A2", "A3"}  # nothing skipped, nothing duplicated
    assert len(page1) + len(page2) == 3


def test_same_second_forward_pagination_with_after_id(client, archive, make_key):
    import os
    import sqlite3
    conn = sqlite3.connect(os.environ["MESSAGES_DB"])
    conn.execute("INSERT INTO messages VALUES (?, 'A0', ?, 900, 0, 'text', 'same second as A1', NULL)",
                 (ALICE, ALICE))
    conn.commit()
    conn.close()
    key = make_key()
    # Page forward from ts=900: without after_id, A0/A1 (both ts 900) get skipped.
    fwd = client.get(f"/v1/chats/{ALICE}/messages",
                     params={"after": 900, "after_id": "A0"},
                     headers=bearer(key)).json()
    ids = {m["id"] for m in fwd}
    assert "A1" in ids and "A2" in ids and "A0" not in ids


def test_media_proxies_sidecar(client, archive, make_key, fake_sidecar):
    key = make_key()
    r = client.get(f"/v1/media/{BOB}/B1", headers=bearer(key))
    assert r.status_code == 200
    assert r.content == b"IMAGEBYTES"
    assert r.headers["content-type"].startswith("image/jpeg")
