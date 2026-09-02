"""Contact listing/search: /v1/contacts."""

from fastapi import APIRouter, Depends

from .. import services
from ..auth import AuthContext
from ..deps import current_auth

router = APIRouter()


@router.get("/v1/contacts")
def list_contacts(q: str = "", limit: int = 50,
                  auth: AuthContext = Depends(current_auth)) -> list[dict]:
    return services.list_contacts(auth, query=q, limit=limit)
