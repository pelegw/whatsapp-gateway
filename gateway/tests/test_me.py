"""GET /v1/me — self-introspection. The sharp edge: it must disclose a key's
own capabilities but NEVER its blind spots (read blocklist / private list)."""

import json
import time

from app import db

from .conftest import ALICE, BOB, admin_headers, bearer


def test_me_reports_role_rate_allowlist_expiry(client, env, make_key):
    key = make_key(name="scout", role="read-send", allowlist=[BOB], rate=9)
    me = client.get("/v1/me", headers=bearer(key)).json()
    assert me["name"] == "scout" and me["role"] == "read-send"
    assert me["rate_per_min"] == 9
    assert me["send_allowlist"] == [BOB]
    assert me["active_grants"] == []
    assert me["key_expires_at"] is None
    # expiring key surfaces its deadline
    r = client.post("/v1/admin/keys", headers=admin_headers(),
                    json={"name": "shortlived", "role": "read-only", "expires_in_days": 2})
    me2 = client.get("/v1/me", headers=bearer(r.json()["key"])).json()
    assert abs(me2["key_expires_at"] - (int(time.time()) + 2 * 86400)) < 60


def test_me_never_leaks_blind_spots(client, archive):
    """read_blocklist and the global private list must not appear anywhere in
    the response, even when both are set — they are the hidden-chat map."""
    r = client.post("/v1/admin/keys", headers=admin_headers(),
                    json={"name": "blinded", "role": "read-only",
                          "read_blocklist": [ALICE]})
    client.post("/v1/admin/privacy/chats", json={"jid": BOB}, headers=admin_headers())
    resp = client.get("/v1/me", headers=bearer(r.json()["key"]))
    body = resp.json()
    assert "read_blocklist" not in body
    raw = json.dumps(body)
    assert ALICE not in raw and BOB not in raw   # neither hidden jid leaks


def test_me_read_allowlist_only_when_set(client, archive):
    plain = client.post("/v1/admin/keys", headers=admin_headers(),
                        json={"name": "plain", "role": "read-only"}).json()["key"]
    assert "read_allowlist" not in client.get("/v1/me", headers=bearer(plain)).json()
    narrow = client.post("/v1/admin/keys", headers=admin_headers(),
                         json={"name": "narrow", "role": "read-only",
                               "read_allowlist": [BOB]}).json()["key"]
    assert client.get("/v1/me", headers=bearer(narrow)).json()["read_allowlist"] == [BOB]


def test_me_active_grants_track_lifecycle(client, env, make_key):
    key = make_key(name="ro", role="read-only")
    gid = client.post("/v1/permissions/request",
                      json={"kind": "send_recipient", "contact": BOB, "duration_hours": 2},
                      headers=bearer(key)).json()["id"]
    assert client.get("/v1/me", headers=bearer(key)).json()["active_grants"] == []  # pending ≠ active
    client.post(f"/v1/admin/grants/{gid}/approve", headers=admin_headers())
    active = client.get("/v1/me", headers=bearer(key)).json()["active_grants"]
    assert [g["id"] for g in active] == [gid] and active[0]["to_jid"] == BOB
    # expiry drops it (sweep runs inside /v1/me)
    with db.connect() as c:
        c.execute("UPDATE grants SET expires_at = ? WHERE id = ?",
                  (int(time.time()) - 5, gid))
    assert client.get("/v1/me", headers=bearer(key)).json()["active_grants"] == []


def test_me_requires_auth_and_writes_no_audit(client, env, make_key):
    assert client.get("/v1/me").status_code == 401
    key = make_key(name="poller", role="read-only")
    for _ in range(5):
        assert client.get("/v1/me", headers=bearer(key)).status_code == 200
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM audit_log WHERE actor = 'poller'").fetchone()[0]
    assert n == 0   # periodic polling must not flood the audit log
