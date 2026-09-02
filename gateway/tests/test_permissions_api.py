"""Agent permission requests over REST + admin decision + end-to-end elevation."""

from .conftest import BOB, admin_headers, bearer


def _request(client, key, **body):
    return client.post("/v1/permissions/request", json=body, headers=bearer(key))


def test_read_only_key_can_request_and_track(client, make_key):
    key = make_key(name="ro", role="read-only")
    r = _request(client, key, kind="send_recipient", contact="+15551234567",
                 reason="reply to a customer")
    assert r.status_code == 201
    gid = r.json()["id"]
    assert r.json()["status"] == "pending"
    # visible to its own key
    assert client.get(f"/v1/permissions/{gid}", headers=bearer(key)).json()["status"] == "pending"
    assert [g["id"] for g in client.get("/v1/permissions", headers=bearer(key)).json()] == [gid]


def test_send_window_requires_duration(client, make_key):
    key = make_key(name="ro2", role="read-only")
    assert _request(client, key, kind="send_window").status_code == 400
    assert _request(client, key, kind="send_recipient").status_code == 400  # missing contact
    assert _request(client, key, kind="bogus", contact="+1").status_code == 422  # Literal


def test_permissions_isolated_per_key(client, make_key):
    a = make_key(name="a", role="read-only")
    b = make_key(name="b", role="read-only")
    gid = _request(client, a, kind="send_recipient", contact="+15551234567").json()["id"]
    assert client.get(f"/v1/permissions/{gid}", headers=bearer(b)).status_code == 404


def test_approve_grant_then_read_only_key_can_send(client, make_key, fake_sidecar):
    key = make_key(name="ro3", role="read-only")
    # read-only cannot send yet
    assert client.post("/v1/send", json={"to": BOB, "text": "hi"},
                       headers=bearer(key)).status_code == 403
    gid = _request(client, key, kind="send_recipient", contact=BOB).json()["id"]
    # admin approves the grant
    assert client.post(f"/v1/admin/grants/{gid}/approve",
                       headers=admin_headers()).json()["status"] == "approved"
    # now the same read-only key delivers directly to that recipient
    r = client.post("/v1/send", json={"to": BOB, "text": "granted!"}, headers=bearer(key))
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert fake_sidecar["send"] == [(BOB, "granted!")]
    # but not to someone else
    assert client.post("/v1/send", json={"to": "+15559999999", "text": "x"},
                       headers=bearer(key)).status_code == 403


def test_admin_grant_lifecycle_and_conflict(client, make_key):
    key = make_key(name="ro4", role="read-only")
    gid = _request(client, key, kind="send_recipient", contact=BOB).json()["id"]
    pending = client.get("/v1/admin/grants", params={"status": "pending"},
                         headers=admin_headers()).json()
    assert [g["id"] for g in pending] == [gid]
    assert pending[0]["key_name"] == "ro4"
    # reject; re-deciding is a 409 conflict
    assert client.post(f"/v1/admin/grants/{gid}/reject", headers=admin_headers()).json()["status"] == "rejected"
    assert client.post(f"/v1/admin/grants/{gid}/approve", headers=admin_headers()).status_code == 409


def test_revoke_active_grant_removes_send(client, make_key, fake_sidecar):
    key = make_key(name="ro5", role="read-only")
    gid = _request(client, key, kind="send_recipient", contact=BOB).json()["id"]
    client.post(f"/v1/admin/grants/{gid}/approve", headers=admin_headers())
    assert client.post("/v1/send", json={"to": BOB, "text": "a"}, headers=bearer(key)).status_code == 200
    # revoke -> read-only again
    assert client.post(f"/v1/admin/grants/{gid}/revoke", headers=admin_headers()).json()["status"] == "revoked"
    assert client.post("/v1/send", json={"to": BOB, "text": "b"}, headers=bearer(key)).status_code == 403


def test_permission_requests_require_a_key(client):
    assert client.post("/v1/permissions/request",
                       json={"kind": "send_window", "duration_hours": 2}).status_code == 401
