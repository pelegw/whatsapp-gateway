"""Authentication and authorization at the REST surface."""

from .conftest import ALICE, admin_headers, bearer


def test_requests_without_key_are_401(client):
    for path in ["/v1/chats", "/v1/contacts", "/v1/drafts",
                 f"/v1/chats/{ALICE}/messages"]:
        assert client.get(path).status_code == 401, path
    assert client.post("/v1/send", json={"to": "1", "text": "x"}).status_code == 401


def test_bogus_key_is_401(client):
    r = client.get("/v1/chats", headers=bearer("wagw_" + "0" * 48))
    assert r.status_code == 401


def test_disabled_key_is_401(client, make_key):
    key = make_key(name="temp")
    from app import db
    with db.connect() as conn:
        conn.execute("UPDATE api_keys SET disabled = 1")
    assert client.get("/v1/chats", headers=bearer(key)).status_code == 401


def test_missing_scope_is_403_with_audit(client, make_key):
    key = make_key(name="readonly", scopes=["read:chats"])
    assert client.get("/v1/chats", headers=bearer(key)).status_code == 200
    r = client.get("/v1/contacts", headers=bearer(key))
    assert r.status_code == 403
    assert "read:contacts" in r.json()["error"]
    from app import db
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM audit_log WHERE action='authz.denied'").fetchone()
    assert row["actor"] == "readonly"


def test_admin_endpoints_reject_agent_keys_and_no_token(client, make_key):
    key = make_key()
    for headers in ({}, bearer(key)):
        assert client.get("/v1/admin/keys", headers=headers).status_code == 401
    assert client.get("/v1/admin/keys", headers=admin_headers()).status_code == 200


def test_health_needs_no_auth(client, fake_sidecar):
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["gateway"] == "ok"
