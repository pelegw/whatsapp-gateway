"""Grants: data access + role-widening in policy.route_send."""

import time

import pytest

from app import admin_services, grants, policy
from app.auth import ROLE_DRAFT, ROLE_READ, ROLE_SEND, AuthContext, scopes_for_role
from app.policy import PolicyError

from .conftest import ALICE, BOB


def _key_id(make_key, name, role="read-only", allowlist=None):
    make_key(name=name, role=role, allowlist=allowlist)
    from app import db
    with db.connect() as c:
        return c.execute("SELECT id FROM api_keys WHERE name = ?", (name,)).fetchone()["id"]


def _ctx(key_id, role, allowlist=()):
    return AuthContext(key_id=key_id, name="t", role=role,
                       scopes=scopes_for_role(role), send_allowlist=list(allowlist))


def _approve(key_id, kind, to_jid=None, expires_at=None):
    g = grants.create(key_id, kind, to_jid, expires_at, "because")
    admin_services.decide_grant(g["id"], approve=True)
    return g


def test_recipient_grant_elevates_read_only(env, make_key):
    kid = _key_id(make_key, "ro")
    _approve(kid, grants.KIND_RECIPIENT, to_jid=BOB)
    # granted recipient -> direct even though the key is read-only
    assert policy.route_send(_ctx(kid, ROLE_READ), BOB) == "direct"
    assert grants.has_active(kid, BOB)
    # a different recipient is still denied
    with pytest.raises(PolicyError) as e:
        policy.route_send(_ctx(kid, ROLE_READ), ALICE)
    assert e.value.status == 403


def test_grant_makes_read_draft_send_directly(env, make_key):
    kid = _key_id(make_key, "planner", role="read-draft")
    assert policy.route_send(_ctx(kid, ROLE_DRAFT), BOB) == "draft"   # before grant
    _approve(kid, grants.KIND_RECIPIENT, to_jid=BOB)
    assert policy.route_send(_ctx(kid, ROLE_DRAFT), BOB) == "direct"  # after grant


def test_window_grant_covers_any_recipient(env, make_key):
    kid = _key_id(make_key, "windowed")
    _approve(kid, grants.KIND_WINDOW, to_jid=None, expires_at=int(time.time()) + 3600)
    assert policy.route_send(_ctx(kid, ROLE_READ), ALICE) == "direct"
    assert policy.route_send(_ctx(kid, ROLE_READ), BOB) == "direct"


def test_expired_grant_falls_back(env, make_key):
    kid = _key_id(make_key, "expiring")
    _approve(kid, grants.KIND_RECIPIENT, to_jid=BOB, expires_at=int(time.time()) - 1)
    assert not grants.has_active(kid, BOB)
    with pytest.raises(PolicyError):
        policy.route_send(_ctx(kid, ROLE_READ), BOB)   # back to read-only denial
    grants.sweep_expired()
    assert grants.list_for_key(kid)[0]["status"] == "expired"


def test_off_allowlist_read_send_uses_grant(env, make_key):
    kid = _key_id(make_key, "butler", role="read-send", allowlist=["+972501111111"])
    ctx = _ctx(kid, ROLE_SEND, allowlist=[ALICE])
    assert policy.route_send(ctx, BOB) == "draft"       # off allowlist, no grant
    _approve(kid, grants.KIND_RECIPIENT, to_jid=BOB)
    assert policy.route_send(ctx, BOB) == "direct"      # now granted
