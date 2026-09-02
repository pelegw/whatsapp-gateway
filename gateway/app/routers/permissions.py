"""Agent-facing permission (grant) requests: /v1/permissions.

An agent asks for a scoped capability; the gateway records it as a pending grant
and notifies the human (Telegram/console) to approve. Approving is human-only
(admin routes), never an agent action.
"""

from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import services
from ..auth import AuthContext
from ..deps import current_auth

router = APIRouter()


class PermissionBody(BaseModel):
    kind: Literal["send_recipient", "send_window"]
    contact: str | None = None                                # required for send_recipient
    duration_hours: int | None = Field(default=None, ge=1, le=8760)
    reason: str = Field(default="", max_length=500)


@router.post("/v1/permissions/request")
def request_permission(body: PermissionBody,
                       auth: AuthContext = Depends(current_auth)) -> JSONResponse:
    grant = services.request_permission(auth, body.kind, body.contact,
                                        body.duration_hours, body.reason)
    return JSONResponse(status_code=201, content=grant)


@router.get("/v1/permissions")
def list_permissions(auth: AuthContext = Depends(current_auth)) -> list[dict]:
    return services.list_my_permissions(auth)


@router.get("/v1/permissions/{grant_id}")
def get_permission(grant_id: str, auth: AuthContext = Depends(current_auth)) -> dict:
    return services.get_permission_status(auth, grant_id)
