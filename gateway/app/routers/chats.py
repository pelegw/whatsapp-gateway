"""Chat and message reads: /v1/chats, /v1/chats/{jid}, /v1/chats/{jid}/messages."""

from fastapi import APIRouter, Depends

from .. import services
from ..auth import AuthContext
from ..deps import current_auth

router = APIRouter()


@router.get("/v1/chats")
def list_chats(q: str = "", limit: int = 50, offset: int = 0,
               auth: AuthContext = Depends(current_auth)) -> list[dict]:
    return services.list_chats(auth, query=q, limit=limit, offset=offset)


@router.get("/v1/chats/{jid}")
def get_chat(jid: str, auth: AuthContext = Depends(current_auth)) -> dict:
    return services.get_chat(auth, jid)


@router.get("/v1/chats/{jid}/messages")
def list_messages(jid: str, limit: int = 50, before: int | None = None,
                  after: int | None = None, before_id: str | None = None,
                  after_id: str | None = None,
                  auth: AuthContext = Depends(current_auth)) -> list[dict]:
    # before_id/after_id refine the ts cursor: the (ts, id) keyset avoids
    # skipping same-second messages at page boundaries in either direction.
    return services.list_messages(auth, jid, limit=limit, before=before,
                                  after=after, before_id=before_id, after_id=after_id)
