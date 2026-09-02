"""Cross-chat message search: /v1/messages/search."""

from fastapi import APIRouter, Depends

from .. import services
from ..auth import AuthContext
from ..deps import current_auth

router = APIRouter()


@router.get("/v1/messages/search")
def search(q: str, chat_jid: str | None = None, limit: int = 20,
           auth: AuthContext = Depends(current_auth)) -> list[dict]:
    return services.search_messages(auth, q, chat_jid, limit)
