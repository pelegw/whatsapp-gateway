"""Read-side chat privacy: global private list + per-key block/allow lists.

The adversarial cases matter most here: a private chat must be invisible via
EVERY read path (list, get, messages, search, media), not just the obvious one.
"""

from app import db

from .conftest import ALICE, BOB, GROUP, admin_headers, bearer


def _make_private(client, jid, reason=""):
    r = client.post("/v1/admin/privacy/chats", json={"jid": jid, "reason": reason},
                    headers=admin_headers())
    assert r.status_code == 201
    return r


# ------------------------------------------------------------ global list

def test_global_private_hides_chat_everywhere(client, archive, make_key):
    key = make_key(name="reader", role="read-only")
    _make_private(client, ALICE, "personal")

    jids = [c["jid"] for c in client.get("/v1/chats", headers=bearer(key)).json()]
    assert ALICE not in jids and BOB in jids and GROUP in jids
    assert client.get(f"/v1/chats/{ALICE}", headers=bearer(key)).status_code == 404
    assert client.get(f"/v1/chats/{ALICE}/messages", headers=bearer(key)).json() == []


def test_global_private_applies_to_full_scope_keys(client, archive, make_key):
    key = make_key(name="powerful", role="read-send")   # global list binds everyone
    _make_private(client, ALICE)
    assert client.get(f"/v1/chats/{ALICE}", headers=bearer(key)).status_code == 404


def test_search_cannot_leak_private_chat(client, archive, make_key):
    # search_messages has no chat_jid filter by default — the bypass most
    # likely to be missed if filtering were bolted onto list paths only.
    key = make_key(name="searcher", role="read-only")
    assert any("lunch" in m["text"] for m in
               client.get("/v1/messages/search?q=lunch", headers=bearer(key)).json())
    _make_private(client, ALICE)
    assert client.get("/v1/messages/search?q=lunch", headers=bearer(key)).json() == []


def test_media_of_private_chat_never_reaches_sidecar(client, archive, make_key, monkeypatch):
    key = make_key(name="reader", role="read-only")
    calls = []
    monkeypatch.setattr("app.sidecar.media",
                        lambda c, m: calls.append((c, m)) or (b"BYTES", "image/jpeg"))
    assert client.get(f"/v1/media/{BOB}/B1", headers=bearer(key)).status_code == 200
    assert calls == [(BOB, "B1")]
    _make_private(client, BOB)
    assert client.get(f"/v1/media/{BOB}/B1", headers=bearer(key)).status_code == 404
    assert calls == [(BOB, "B1")]            # sidecar was NOT called a second time


def test_name_search_respects_filter_or_parens_regression(client, archive, make_key):
    # Under the unparenthesized OR bug, `name LIKE ? OR jid LIKE ? AND jid NOT
    # IN (...)` would return Alice via the name arm despite the deny list.
    key = make_key(name="reader", role="read-only")
    _make_private(client, ALICE)
    assert client.get("/v1/chats?q=Alice", headers=bearer(key)).json() == []
    assert [c["jid"] for c in client.get("/v1/chats?q=Bob", headers=bearer(key)).json()] == [BOB]


def test_contacts_stay_visible_by_design(client, archive, make_key):
    # v1 decision: contacts are the name→JID resolver for sending, so hiding a
    # CHAT does not hide the CONTACT. Locked in so a change is deliberate.
    key = make_key(name="reader", role="read-only")
    _make_private(client, ALICE)
    names = [c["full_name"] for c in
             client.get("/v1/contacts?q=Alice", headers=bearer(key)).json()]
    assert "Alice Cohen" in names


# ------------------------------------------------------------ per-key lists

def _key_via_admin(client, name, **extra):
    body = {"name": name, "role": "read-only", **extra}
    r = client.post("/v1/admin/keys", json=body, headers=admin_headers())
    assert r.status_code == 200
    return r.json()["key"]


def test_per_key_blocklist_hides_for_that_key_only(client, archive):
    blocked = _key_via_admin(client, "blocked", read_blocklist=[ALICE])
    free = _key_via_admin(client, "free")
    assert client.get(f"/v1/chats/{ALICE}", headers=bearer(blocked)).status_code == 404
    assert client.get(f"/v1/chats/{ALICE}", headers=bearer(free)).status_code == 200


def test_per_key_allowlist_restricts_to_exactly_those(client, archive):
    key = _key_via_admin(client, "narrow", read_allowlist=[BOB])
    jids = [c["jid"] for c in client.get("/v1/chats", headers=bearer(key)).json()]
    assert jids == [BOB]
    assert client.get(f"/v1/chats/{GROUP}", headers=bearer(key)).status_code == 404
    assert client.get(f"/v1/chats/{BOB}", headers=bearer(key)).status_code == 200


def test_patch_key_read_lists(client, archive):
    key = _key_via_admin(client, "patched")
    key_id = next(k["id"] for k in
                  client.get("/v1/admin/keys", headers=admin_headers()).json()
                  if k["name"] == "patched")
    assert client.get(f"/v1/chats/{ALICE}", headers=bearer(key)).status_code == 200
    r = client.patch(f"/v1/admin/keys/{key_id}", json={"read_blocklist": [ALICE]},
                     headers=admin_headers())
    assert r.status_code == 200
    assert client.get(f"/v1/chats/{ALICE}", headers=bearer(key)).status_code == 404


# ------------------------------------------------------------ name picker

def test_resolve_requires_admin(client, archive, make_key):
    key = make_key(name="agent", role="read-send")       # even a powerful AGENT key
    assert client.get("/v1/admin/privacy/resolve?q=fam").status_code in (401, 403)
    assert client.get("/v1/admin/privacy/resolve?q=fam",
                      headers=bearer(key)).status_code in (401, 403)


def test_resolve_finds_groups_and_contacts(client, archive):
    r = client.get("/v1/admin/privacy/resolve?q=fam", headers=admin_headers()).json()
    assert [(m["jid"], m["kind"]) for m in r] == [(GROUP, "group")]
    # a DM chat and its contact share a jid — deduped, chat wins
    r = client.get("/v1/admin/privacy/resolve?q=alice", headers=admin_headers()).json()
    assert [(m["jid"], m["kind"]) for m in r] == [(ALICE, "chat")]
    # address-book-only match (full_name) surfaces as a contact
    r = client.get("/v1/admin/privacy/resolve?q=cohen", headers=admin_headers()).json()
    assert [(m["jid"], m["kind"]) for m in r] == [(ALICE, "contact")]
    assert client.get("/v1/admin/privacy/resolve?q=", headers=admin_headers()).json() == []


def test_rename_cannot_unhide(client, archive, make_key):
    """Enforcement is by pinned jid: a group member renaming the chat (or a
    contact changing their push name) must not resurface a private chat."""
    import os
    import sqlite3
    key = make_key(name="reader", role="read-only")
    client.post("/v1/admin/privacy/chats", json={"jid": GROUP, "name": "Family"},
                headers=admin_headers())
    assert client.get(f"/v1/chats/{GROUP}", headers=bearer(key)).status_code == 404
    conn = sqlite3.connect(os.environ["MESSAGES_DB"])
    conn.execute("UPDATE chats SET name = 'Totally Different' WHERE jid = ?", (GROUP,))
    conn.commit(); conn.close()
    assert client.get(f"/v1/chats/{GROUP}", headers=bearer(key)).status_code == 404
    assert GROUP not in [c["jid"] for c in
                         client.get("/v1/chats", headers=bearer(key)).json()]
    # the stored display name survives for the admin list
    rows = client.get("/v1/admin/privacy/chats", headers=admin_headers()).json()
    assert rows[0]["name"] == "Family"


# ------------------------------------------------------------ admin CRUD

def test_private_list_crud_and_audit(client, env):
    _make_private(client, "+15551230000", "sensitive")
    rows = client.get("/v1/admin/privacy/chats", headers=admin_headers()).json()
    assert rows[0]["jid"] == "15551230000@s.whatsapp.net"    # normalized
    r = client.delete("/v1/admin/privacy/chats/15551230000@s.whatsapp.net",
                      headers=admin_headers())
    assert r.status_code == 200
    assert client.get("/v1/admin/privacy/chats", headers=admin_headers()).json() == []
    r = client.delete("/v1/admin/privacy/chats/15551230000@s.whatsapp.net",
                      headers=admin_headers())
    assert r.status_code == 404
    with db.connect() as c:
        actions = [r["action"] for r in c.execute(
            "SELECT action FROM audit_log WHERE action LIKE 'privacy.%'")]
    assert actions == ["privacy.chat_added", "privacy.chat_removed"]
