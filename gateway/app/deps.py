"""FastAPI dependencies: agent-key auth and admin (token + Cloudflare Access)."""

import secrets

from fastapi import Header, HTTPException, Request

from . import auth, cf_access
from .auth import AuthContext
from .config import get_settings


def _client_ip(request: Request) -> str:
    """Real client IP, as stamped by OriginGuardMiddleware (CF-Connecting-IP
    when the request is trusted, else the socket peer)."""
    ip = request.scope.get("state", {}).get("client_ip")
    if ip:
        return ip
    return request.client.host if request.client else ""


def current_auth(request: Request,
                 authorization: str | None = Header(None)) -> AuthContext:
    ctx = auth.authenticate_bearer(authorization, _client_ip(request))
    if ctx is None:
        raise HTTPException(401, "missing or invalid API key (Authorization: Bearer wagw_...)")
    return ctx


def require_admin(
    request: Request,
    authorization: str | None = Header(None),
    cf_access_jwt_assertion: str | None = Header(None),
) -> None:
    """Guard the management plane. Always checks the admin token; when
    Cloudflare Access is enabled, ALSO requires a valid Access identity so the
    admin surface is unreachable by anyone bypassing Cloudflare."""
    s = get_settings()

    expected = s.admin_token
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    # compare_digest guards the admin token against timing probes; compare as
    # bytes so a non-ASCII header value yields a clean 401, not a TypeError->500.
    if not expected or not secrets.compare_digest(
            supplied.encode("latin1", "ignore"), expected.encode()):
        raise HTTPException(401, "admin token required (Authorization: Bearer <ADMIN_TOKEN>)")

    if s.cf_access_enabled:
        # Header name maps: Cf-Access-Jwt-Assertion -> cf_access_jwt_assertion.
        try:
            cf_access.verify(cf_access_jwt_assertion)
        except cf_access.AccessError as e:
            raise HTTPException(403, f"Cloudflare Access: {e}") from e
