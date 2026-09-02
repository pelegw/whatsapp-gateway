"""Internet-exposure security: origin lockdown, Cloudflare Access, key lifecycle."""

import time
import types

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from .conftest import ALICE, admin_headers, bearer

TEAM = "myteam.cloudflareaccess.com"
AUD = "test-access-aud"


# ---------------------------------------------------------------- origin guard

def _fresh_client():
    from app.config import get_settings
    from app.main import app
    get_settings.cache_clear()
    return TestClient(app)


def test_origin_secret_blocks_requests_without_the_header(env, monkeypatch, make_key):
    monkeypatch.setenv("ORIGIN_SECRET", "s3cret-edge-token")
    key = make_key(scopes=["read:chats"])
    client = _fresh_client()

    # No edge header -> looks like a direct-to-origin hit -> 403.
    assert client.get("/v1/chats", headers=bearer(key)).status_code == 403
    # Wrong value -> 403.
    assert client.get("/v1/chats", headers={**bearer(key), "x-wagw-origin": "nope"}
                      ).status_code == 403
    # Correct edge secret -> allowed through to normal auth.
    assert client.get("/v1/chats", headers={**bearer(key), "x-wagw-origin": "s3cret-edge-token"}
                      ).status_code == 200


def test_health_is_exempt_from_origin_secret(env, monkeypatch, fake_sidecar):
    monkeypatch.setenv("ORIGIN_SECRET", "s3cret-edge-token")
    client = _fresh_client()
    assert client.get("/v1/health").status_code == 200  # probes work without the secret


def test_cf_connecting_ip_recorded_only_when_trusted(env, monkeypatch, make_key):
    monkeypatch.setenv("ORIGIN_SECRET", "s3cret-edge-token")
    key = make_key(scopes=["read:chats"])
    client = _fresh_client()
    edge = {"x-wagw-origin": "s3cret-edge-token"}
    client.get("/v1/chats", headers={**bearer(key), **edge, "cf-connecting-ip": "203.0.113.9"})
    # The admin request also arrives through the edge, so it carries the secret too.
    keys = client.get("/v1/admin/keys", headers={**admin_headers(), **edge}).json()
    assert keys[0]["last_used_ip"] == "203.0.113.9"


# --------------------------------------------------------------- cloudflare access

@pytest.fixture()
def cf_identity(env, monkeypatch):
    """Enable CF Access and mint valid RS256 tokens against a mocked JWKS."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()

    from app import cf_access
    cf_access.reset_cache()
    monkeypatch.setattr(cf_access, "_jwks", lambda _domain: types.SimpleNamespace(
        get_signing_key_from_jwt=lambda _t: types.SimpleNamespace(key=pub)))

    monkeypatch.setenv("CF_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    monkeypatch.setenv("CF_ACCESS_ALLOWED_EMAILS", "peleg@wasserman.me")
    from app.config import get_settings
    get_settings.cache_clear()

    def make_token(**overrides):
        claims = {"aud": AUD, "iss": f"https://{TEAM}",
                  "exp": int(time.time()) + 3600, "email": "peleg@wasserman.me"}
        claims.update(overrides)
        return jwt.encode(claims, priv, algorithm="RS256")

    return make_token


def _cf_client():
    from app.main import app
    return TestClient(app)


def test_admin_requires_valid_access_jwt(cf_identity, fake_sidecar):
    client = _cf_client()
    good = {**admin_headers(), "cf-access-jwt-assertion": cf_identity()}
    assert client.get("/v1/admin/keys", headers=good).status_code == 200


def test_admin_token_alone_is_rejected_when_access_enabled(cf_identity):
    client = _cf_client()
    # Correct admin token but no Access identity -> 403 (bypass attempt).
    assert client.get("/v1/admin/keys", headers=admin_headers()).status_code == 403


def test_access_rejects_bad_tokens(cf_identity):
    client = _cf_client()
    cases = {
        "expired": cf_identity(exp=int(time.time()) - 10),
        "wrong_aud": cf_identity(aud="someone-elses-app"),
        "wrong_issuer": cf_identity(iss="https://evil.cloudflareaccess.com"),
        "email_not_allowed": cf_identity(email="stranger@example.com"),
        "missing_exp": cf_identity(exp=None),   # exp must be present, not just valid
        "garbage": "not.a.jwt",
    }
    for label, token in cases.items():
        r = client.get("/v1/admin/keys",
                       headers={**admin_headers(), "cf-access-jwt-assertion": token})
        assert r.status_code == 403, label


def test_access_still_needs_the_admin_token(cf_identity):
    client = _cf_client()
    # Valid identity but no admin token -> 401 (token is the service credential).
    assert client.get("/v1/admin/keys",
                      headers={"cf-access-jwt-assertion": cf_identity()}).status_code == 401


# ------------------------------------------------------------- key lifecycle

def test_expired_key_is_rejected(client, make_key):
    from app import db
    key = make_key(scopes=["read:chats"])
    with db.connect() as conn:
        conn.execute("UPDATE api_keys SET expires_at = ?", (int(time.time()) - 1,))
    assert client.get("/v1/chats", headers=bearer(key)).status_code == 401


def test_create_key_with_expiry(client):
    r = client.post("/v1/admin/keys", headers=admin_headers(),
                    json={"name": "temp", "scopes": ["read:chats"], "expires_in_days": 7})
    assert r.status_code == 200
    assert r.json()["expires_at"] > int(time.time())


def test_rotation_keeps_old_key_during_grace_then_kills_it(client, make_key):
    from app import db
    old = make_key(name="rot", scopes=["read:chats"])
    key_id = client.get("/v1/admin/keys", headers=admin_headers()).json()[0]["id"]

    new = client.post(f"/v1/admin/keys/{key_id}/rotate", headers=admin_headers()).json()["key"]
    assert new != old and new.startswith("wagw_")

    # During the grace window BOTH secrets authenticate.
    assert client.get("/v1/chats", headers=bearer(new)).status_code == 200
    assert client.get("/v1/chats", headers=bearer(old)).status_code == 200
    assert client.get("/v1/admin/keys", headers=admin_headers()).json()[0]["rotating"] is True

    # After the grace window the OLD secret stops working; the new one persists.
    with db.connect() as conn:
        conn.execute("UPDATE api_keys SET prev_expires_at = ?", (int(time.time()) - 1,))
    assert client.get("/v1/chats", headers=bearer(old)).status_code == 401
    assert client.get("/v1/chats", headers=bearer(new)).status_code == 200


def test_last_used_is_tracked(client, make_key):
    key = make_key(scopes=["read:chats"])
    client.get("/v1/chats", headers=bearer(key))
    row = client.get("/v1/admin/keys", headers=admin_headers()).json()[0]
    assert row["last_used_at"] is not None


# ------------------------------------------------------ fail-closed interlock

def _settings(**over):
    from app.config import Settings
    base = dict(origin_secret="", cf_access_enabled=False, cf_access_team_domain="",
                cf_access_aud="", allow_insecure_admin=False)
    base.update(over)
    return Settings(**base)


def test_public_mode_without_access_refuses_to_start(env):
    from app.config import validate_exposure
    # ORIGIN_SECRET set but no Cloudflare Access -> boot must fail closed.
    with pytest.raises(RuntimeError, match="admin plane"):
        validate_exposure(_settings(origin_secret="edge"))


def test_public_mode_with_access_is_allowed(env):
    from app.config import validate_exposure
    validate_exposure(_settings(origin_secret="edge", cf_access_enabled=True,
                                cf_access_team_domain="t.cloudflareaccess.com",
                                cf_access_aud="aud"))  # no raise


def test_explicit_override_allows_insecure_admin(env):
    from app.config import validate_exposure
    validate_exposure(_settings(origin_secret="edge", allow_insecure_admin=True))


def test_access_enabled_but_unconfigured_refuses_to_start(env):
    from app.config import validate_exposure
    with pytest.raises(RuntimeError, match="CF_ACCESS"):
        validate_exposure(_settings(cf_access_enabled=True))


def test_local_default_is_fine(env):
    from app.config import validate_exposure
    validate_exposure(_settings())  # nothing set -> no raise


# ------------------------------------------------------ misc hardening

def test_non_ascii_origin_header_is_rejected_not_500(env, monkeypatch):
    # Raw non-ASCII header bytes only exist at the ASGI layer (httpx blocks
    # them), so drive the middleware directly. A byte like 0xFF must yield a
    # clean 403, never a TypeError -> 500 on every request.
    import asyncio

    monkeypatch.setenv("ORIGIN_SECRET", "s3cret-edge-token")
    from app.config import get_settings
    from app.origin import OriginGuardMiddleware
    get_settings.cache_clear()

    async def dummy(scope, receive, send):  # would only run if the guard passed
        raise AssertionError("request should have been blocked")

    guard = OriginGuardMiddleware(dummy)
    scope = {"type": "http", "path": "/v1/chats", "client": ("198.51.100.7", 5),
             "headers": [(b"x-wagw-origin", b"\xff\xfe")]}
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request"}

    asyncio.run(guard(scope, receive, send))
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 403


def test_health_does_not_leak_link_status(client):
    body = client.get("/v1/health").json()
    assert body == {"gateway": "ok"}  # no sidecar/whatsapp_logged_in/archive_present


def test_non_ascii_admin_header_denies_cleanly(env):
    # require_admin must reject a non-ASCII Authorization header with 401,
    # never raise TypeError -> 500. Call the dependency directly (httpx can't
    # transmit non-ASCII headers).
    from fastapi import HTTPException
    from app.deps import require_admin
    from app.config import get_settings
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as e:
        require_admin(request=None, authorization="Bearer \xff\xfe",
                      cf_access_jwt_assertion=None)
    assert e.value.status_code == 401
