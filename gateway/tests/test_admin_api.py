"""Admin surface: key management, the approve/reject flow, audit, status."""

from .conftest import ALICE, BOB, admin_headers, bearer

SEND_SCOPES = ["send:direct", "send:draft", "drafts:read"]


def make_pending_draft(client, key, to=BOB, text="please approve"):
    r = client.post("/v1/drafts", json={"to": to, "text": text}, headers=bearer(key))
    assert r.status_code == 201
    return r.json()["id"]


def test_key_lifecycle_via_rest(client):
    r = client.post("/v1/admin/keys", headers=admin_headers(), json={
        "name": "claude", "scopes": ["read:chats", "send:draft"],
        "allowlist": ["+972501111111"]})
    assert r.status_code == 200
    key = r.json()["key"]
    assert key.startswith("wagw_")

    rows = client.get("/v1/admin/keys", headers=admin_headers()).json()
    assert rows[0]["name"] == "claude"
    assert rows[0]["send_allowlist"] == [ALICE]  # normalized at creation
    assert "key_hash" not in rows[0]             # hashes stay internal

    # The fresh key authenticates; disabling kills it immediately.
    assert client.get("/v1/chats", headers=bearer(key)).status_code == 200
    client.patch(f"/v1/admin/keys/{rows[0]['id']}", headers=admin_headers(),
                 json={"disabled": True})
    assert client.get("/v1/chats", headers=bearer(key)).status_code == 401


def test_create_key_rejects_unknown_scope(client):
    r = client.post("/v1/admin/keys", headers=admin_headers(),
                    json={"name": "x", "scopes": ["send:everything"]})
    assert r.status_code == 400


def test_approve_sends_and_marks_draft(client, make_key, fake_sidecar):
    key = make_key(scopes=SEND_SCOPES)
    draft_id = make_pending_draft(client, key)

    pending = client.get("/v1/admin/drafts", params={"status": "pending"},
                         headers=admin_headers()).json()
    assert [d["id"] for d in pending] == [draft_id]
    assert pending[0]["key_name"] == "agent"  # human sees who asked

    r = client.post(f"/v1/admin/drafts/{draft_id}/approve", headers=admin_headers())
    assert r.status_code == 200
    assert r.json()["status"] == "sent"
    assert fake_sidecar["send"] == [(BOB, "please approve")]

    # Agent can see the outcome; a decided draft can't be decided again.
    assert client.get(f"/v1/drafts/{draft_id}", headers=bearer(key)).json()["status"] == "sent"
    assert client.post(f"/v1/admin/drafts/{draft_id}/approve",
                       headers=admin_headers()).status_code == 409


def test_reject_never_sends(client, make_key, fake_sidecar):
    key = make_key(scopes=SEND_SCOPES)
    draft_id = make_pending_draft(client, key)
    r = client.post(f"/v1/admin/drafts/{draft_id}/reject", headers=admin_headers())
    assert r.json()["status"] == "rejected"
    assert fake_sidecar["send"] == []


def test_approve_during_outage_keeps_draft_pending(client, make_key, fake_sidecar):
    # Sidecar down / WhatsApp unlinked (503) is transient: the draft must stay
    # approvable so the human can retry once the link is back.
    key = make_key(scopes=SEND_SCOPES)
    draft_id = make_pending_draft(client, key)
    fake_sidecar["make_send_fail"](503)
    r = client.post(f"/v1/admin/drafts/{draft_id}/approve", headers=admin_headers())
    assert r.status_code == 503
    assert client.get(f"/v1/drafts/{draft_id}", headers=bearer(key)).json()["status"] == "pending"


def test_approve_hard_failure_marks_draft_failed(client, make_key, fake_sidecar):
    key = make_key(scopes=SEND_SCOPES)
    draft_id = make_pending_draft(client, key)
    fake_sidecar["make_send_fail"](400)  # e.g. sidecar rejected the recipient
    r = client.post(f"/v1/admin/drafts/{draft_id}/approve", headers=admin_headers())
    assert r.status_code == 502
    assert client.get(f"/v1/drafts/{draft_id}", headers=bearer(key)).json()["status"] == "failed"


def test_rejected_draft_cannot_be_approved(client, make_key, fake_sidecar):
    # The atomic claim means a decision can never override an earlier one.
    key = make_key(scopes=SEND_SCOPES)
    draft_id = make_pending_draft(client, key)
    client.post(f"/v1/admin/drafts/{draft_id}/reject", headers=admin_headers())
    r = client.post(f"/v1/admin/drafts/{draft_id}/approve", headers=admin_headers())
    assert r.status_code == 409
    assert fake_sidecar["send"] == []


def test_audit_trail_tells_the_story(client, fake_sidecar):
    # Create the key through the admin API so key creation itself is audited.
    key = client.post("/v1/admin/keys", headers=admin_headers(), json={
        "name": "agent", "scopes": SEND_SCOPES, "allowlist": [ALICE],
    }).json()["key"]
    client.post("/v1/send", json={"to": ALICE, "text": "hi"}, headers=bearer(key))
    draft_id = make_pending_draft(client, key)
    client.post(f"/v1/admin/drafts/{draft_id}/approve", headers=admin_headers())

    entries = client.get("/v1/admin/audit", headers=admin_headers()).json()
    actions = [e["action"] for e in entries]
    for expected in ["send.sent", "draft.created", "key.created"]:
        assert expected in actions, actions
    # Admin's approval is attributed to admin, on behalf of the agent key.
    approved = [e for e in entries if e["actor"] == "admin" and e["action"] == "send.sent"]
    assert len(approved) == 1


def test_negative_audit_limit_is_clamped(client, make_key, fake_sidecar):
    # Sibling of the read-limit clamp: LIMIT -1 would dump the entire audit log.
    key = make_key(scopes=["read:chats"])
    for _ in range(3):
        client.get("/v1/chats", headers=bearer(key))
    rows = client.get("/v1/admin/audit", params={"limit": -1},
                      headers=admin_headers()).json()
    assert len(rows) == 1  # clamped to 1, not unbounded


def test_rate_limited_approval_keeps_draft_pending(client, make_key, fake_sidecar):
    # A 429 at approval time is not a bad draft: the claim must be released so
    # the human can approve again once the window clears.
    key = make_key(scopes=SEND_SCOPES, allowlist=[ALICE], rate=1)
    # Burn the per-minute budget with one direct send.
    client.post("/v1/send", json={"to": ALICE, "text": "x"}, headers=bearer(key))
    draft_id = make_pending_draft(client, key, to=ALICE)
    r = client.post(f"/v1/admin/drafts/{draft_id}/approve", headers=admin_headers())
    assert r.status_code == 429
    assert client.get(f"/v1/drafts/{draft_id}", headers=bearer(key)).json()["status"] == "pending"


def test_status_and_approvals_page(client, fake_sidecar):
    st = client.get("/v1/admin/status", headers=admin_headers()).json()
    assert st["sidecar"] == "up" and st["logged_in"] is True
    page = client.get("/admin/approvals")
    assert page.status_code == 200
    assert "WA_GW admin" in page.text
    assert "Log out" in page.text  # dashboard has a logout control
    assert client.get("/admin").status_code == 200  # cleaner alias
