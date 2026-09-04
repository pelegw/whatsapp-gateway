"""Self-introspection: GET /v1/me — what this key may do, for agents that want
to check their access up front (or re-check when a grant nears expiry) instead
of probing by trial and error."""

from fastapi import APIRouter, Depends

from .. import services
from ..auth import AuthContext
from ..deps import current_auth

router = APIRouter()


@router.get("/v1/me")
def me(auth: AuthContext = Depends(current_auth)) -> dict:
    return services.get_my_access(auth)
