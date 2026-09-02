"""Role-based permissions: read-only (default), read-draft, read-send."""

from .conftest import ALICE, BOB, admin_headers, bearer


def _create(client, name, role=None, allowlist=None):
    body = {"name": name}
    if role is not None:
        body["role"] = role
    if allowlist is not None:
        body["allowlist"] = allowlist
    r = client.post("/v1/admin/keys", headers=admin_headers(), json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_default_role_is_read_only(client):
    created = _create(client, "defaulted")
    assert created["role"] == "read-only"


def test_read_only_can_read_but_not_send_or_draft(client, archive, fake_sidecar):
    key = _create(client, "reader", role="read-only")["key"]
    assert client.get("/v1/chats", headers=bearer(key)).status_code == 200
    # No send, no draft.
    assert client.post("/v1/send", json={"to": BOB, "text": "x"},
                       headers=bearer(key)).status_code == 403
    assert client.post("/v1/drafts", json={"to": BOB, "text": "x"},
                       headers=bearer(key)).status_code == 403
    assert fake_sidecar["send"] == []


def test_read_draft_can_draft_but_not_send(client, fake_sidecar):
    key = _create(client, "planner", role="read-draft")["key"]
    # A send is accepted but routed to approval, never delivered.
    r = client.post("/v1/send", json={"to": BOB, "text": "hi"}, headers=bearer(key))
    assert r.status_code == 202 and r.json()["status"] == "pending_approval"
    # Explicit drafts work too.
    assert client.post("/v1/drafts", json={"to": BOB, "text": "hi"},
                       headers=bearer(key)).status_code == 201
    assert fake_sidecar["send"] == []  # nothing auto-sent


def test_read_send_delivers_immediately(client, fake_sidecar):
    key = _create(client, "butler", role="read-send")["key"]
    r = client.post("/v1/send", json={"to": BOB, "text": "on your behalf"},
                    headers=bearer(key))
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert fake_sidecar["send"] == [(BOB, "on your behalf")]


def test_read_send_with_allowlist_drafts_off_list(client, fake_sidecar):
    key = _create(client, "butler2", role="read-send", allowlist=["+972501111111"])["key"]
    # Allowlisted -> delivered.
    assert client.post("/v1/send", json={"to": ALICE, "text": "hey"},
                       headers=bearer(key)).status_code == 200
    # Off the allowlist -> becomes a draft, not delivered.
    r = client.post("/v1/send", json={"to": BOB, "text": "who?"}, headers=bearer(key))
    assert r.status_code == 202 and r.json()["status"] == "pending_approval"
    assert fake_sidecar["send"] == [(ALICE, "hey")]


def test_role_shown_and_changeable(client, fake_sidecar):
    key = _create(client, "upgradeable", role="read-only")["key"]
    kid = client.get("/v1/admin/keys", headers=admin_headers()).json()[-1]["id"]
    assert client.post("/v1/send", json={"to": BOB, "text": "x"},
                       headers=bearer(key)).status_code == 403
    # Promote to read-send; the same key can now deliver.
    client.patch(f"/v1/admin/keys/{kid}", headers=admin_headers(),
                 json={"role": "read-send"})
    assert client.post("/v1/send", json={"to": BOB, "text": "now"},
                       headers=bearer(key)).status_code == 200
    row = client.get("/v1/admin/keys", headers=admin_headers()).json()[-1]
    assert row["role"] == "read-send"


def test_unknown_role_rejected(client):
    r = client.post("/v1/admin/keys", headers=admin_headers(),
                    json={"name": "bad", "role": "superuser"})
    assert r.status_code == 422  # pydantic Literal rejects it


def test_direct_send_requires_scope_even_if_role_drifts(env, fake_sidecar):
    # Defense in depth: a hand-built context whose role says read-send but whose
    # scopes lack send:direct must NOT deliver — the scope check fails closed.
    from app import services
    from app.auth import AuthContext, ROLE_SEND
    from app.policy import PolicyError

    drifted = AuthContext(key_id=1, name="drifted", role=ROLE_SEND,
                          scopes=["read:chats"], send_allowlist=[])
    try:
        services.send_message(drifted, "+15551234567", "should not send")
        assert False, "expected a scope denial"
    except PolicyError as e:
        assert e.status == 403
    assert fake_sidecar["send"] == []  # nothing delivered
