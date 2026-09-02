"""GET /v1/health — unauthenticated liveness only.

Deliberately minimal: it is exempt from the origin secret (so orchestration
probes work) and therefore reachable by anonymous internet callers, so it must
NOT disclose WhatsApp link state, sidecar status, or archive presence. Those
live on the authenticated GET /v1/admin/status instead."""

from fastapi import APIRouter

router = APIRouter()


# Both paths return the same liveness. /health is the conventional root probe
# for load balancers / uptime monitors; /v1/health keeps the API namespace.
@router.get("/health", include_in_schema=False)
@router.get("/v1/health")
def health() -> dict:
    return {"gateway": "ok"}
