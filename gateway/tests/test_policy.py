"""Unit tests for the policy engine: JID normalization, routing, rate limits."""

import pytest

from app import audit, policy
from app.auth import AuthContext
from app.policy import PolicyError

ALICE = "972501111111@s.whatsapp.net"


def test_normalize_jid_accepts_phone_and_jids():
    assert policy.normalize_jid("+972501111111") == ALICE
    assert policy.normalize_jid("972501111111") == ALICE
    assert policy.normalize_jid(ALICE) == ALICE
    assert policy.normalize_jid("123@g.us") == "123@g.us"


def test_normalize_jid_lid_and_device_suffix():
    # Hidden-user chats (@lid) come out of the archive and must be sendable.
    assert policy.normalize_jid("123456789012345@lid") == "123456789012345@lid"
    # ":device" and ".agent" suffixes must not bypass exact allowlist
    # comparison — the gateway must canonicalize exactly like the sidecar's
    # ToNonAD(), or the approved JID differs from the delivered one.
    assert policy.normalize_jid("972501111111:12@s.whatsapp.net") == ALICE
    assert policy.normalize_jid("972501111111.0@s.whatsapp.net") == ALICE
    assert policy.normalize_jid("972501111111.0:2@s.whatsapp.net") == ALICE
    assert policy.normalize_jid("123456789012345:9@lid") == "123456789012345@lid"


@pytest.mark.parametrize("bad", ["", "not-a-number", "x@example.com", "@s.whatsapp.net"])
def test_normalize_jid_rejects_garbage(bad):
    with pytest.raises(PolicyError) as e:
        policy.normalize_jid(bad)
    assert e.value.status == 400


def _ctx(role, allowlist=(), rate=6):
    from app.auth import scopes_for_role
    return AuthContext(key_id=1, name="t", role=role,
                       scopes=scopes_for_role(role),
                       send_allowlist=list(allowlist), rate_per_min=rate)


def test_read_send_delivers_directly(env):
    # No allowlist -> auto-send to anyone.
    assert policy.route_send(_ctx("read-send"), ALICE) == "direct"
    # With an allowlist -> auto-send only to listed recipients.
    assert policy.route_send(_ctx("read-send", [ALICE]), ALICE) == "direct"


def test_read_send_off_allowlist_falls_to_draft(env):
    # A restricted read-send key messaging someone off its list needs approval.
    assert policy.route_send(_ctx("read-send", [ALICE]), "999@s.whatsapp.net") == "draft"


def test_read_draft_always_drafts(env):
    assert policy.route_send(_ctx("read-draft"), ALICE) == "draft"


def test_read_only_cannot_send(env):
    with pytest.raises(PolicyError) as e:
        policy.route_send(_ctx("read-only"), ALICE)
    assert e.value.status == 403


def test_rate_limiter_per_key_window():
    rl = policy.RateLimiter()
    assert all(rl.check(1, 3) for _ in range(3))
    assert not rl.check(1, 3)          # 4th within a minute: over budget
    assert rl.check(2, 3)              # other keys unaffected


def test_enforce_rate_limits_global_daily_cap(env, monkeypatch):
    monkeypatch.setenv("GLOBAL_SENDS_PER_DAY", "2")
    from app.config import get_settings
    get_settings.cache_clear()
    for _ in range(2):
        audit.audit("someone", "send.sent")
    with pytest.raises(PolicyError) as e:
        policy.enforce_rate_limits(_ctx("read-send"), "someone")
    assert e.value.status == 429
    assert "global" in str(e.value)
