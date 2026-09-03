"""POST /v1/send — the policy-routed send endpoint.

200 {"status":"sent",...}              delivered (allowlisted + send:direct)
202 {"status":"pending_approval",...}  became a draft, human must approve
403 / 429                              denied / rate limited (via PolicyError)
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import services
from ..auth import AuthContext
from ..deps import current_auth

router = APIRouter()


class SendBody(BaseModel):
    to: str = Field(description="JID (…@s.whatsapp.net / …@g.us) or international phone number")
    text: str = Field(min_length=1, max_length=8000)
    # Scheduling (either one, not both): deliver later instead of now.
    send_at: int | None = Field(default=None, description="unix ts to deliver at")
    delay_seconds: int | None = Field(default=None, ge=1)


@router.post("/v1/send")
def send(body: SendBody, auth: AuthContext = Depends(current_auth)) -> JSONResponse:
    result = services.send_message(auth, body.to, body.text,
                                   send_at=body.send_at, delay_seconds=body.delay_seconds)
    status = 202 if result["status"] in ("pending_approval", "scheduled") else 200
    return JSONResponse(status_code=status, content=result)
