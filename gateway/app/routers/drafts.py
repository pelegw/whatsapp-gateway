"""Agent-facing draft management: create, list own, inspect, cancel."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import services
from ..auth import AuthContext
from ..deps import current_auth

router = APIRouter()


class DraftBody(BaseModel):
    to: str
    text: str = Field(min_length=1, max_length=8000)
    note: str = Field(default="", max_length=500,
                      description="Why you wrote this — shown to the approving human")


@router.post("/v1/drafts")
def create_draft(body: DraftBody, auth: AuthContext = Depends(current_auth)) -> JSONResponse:
    draft = services.create_draft(auth, body.to, body.text, body.note)
    return JSONResponse(status_code=201, content=draft)


@router.get("/v1/drafts")
def list_drafts(auth: AuthContext = Depends(current_auth)) -> list[dict]:
    return services.list_my_drafts(auth)


@router.get("/v1/drafts/{draft_id}")
def get_draft(draft_id: str, auth: AuthContext = Depends(current_auth)) -> dict:
    return services.get_draft(auth, draft_id)


@router.delete("/v1/drafts/{draft_id}")
def cancel_draft(draft_id: str, auth: AuthContext = Depends(current_auth)) -> dict:
    return services.cancel_draft(auth, draft_id)
