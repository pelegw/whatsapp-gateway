"""Media download proxy: /v1/media/{chat_jid}/{message_id}."""

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from .. import services
from ..auth import AuthContext
from ..deps import current_auth

router = APIRouter()


@router.get("/v1/media/{chat_jid}/{message_id}")
def get_media(chat_jid: str, message_id: str,
              auth: AuthContext = Depends(current_auth)) -> Response:
    data, content_type = services.get_media(auth, chat_jid, message_id)
    return Response(content=data, media_type=content_type)
