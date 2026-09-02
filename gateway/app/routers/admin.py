"""Human-only admin surface (ADMIN_TOKEN): approvals, keys, audit, WhatsApp
status/QR proxy, and the approvals HTML page. Never expose this beyond
localhost/LAN without extra transport protection."""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from .. import admin_services, sidecar
from ..deps import require_admin

router = APIRouter(dependencies=[Depends(require_admin)])
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


# ------------------------------------------------------------ approvals

@router.get("/v1/admin/drafts")
def list_drafts(status: str | None = None) -> list[dict]:
    return admin_services.list_drafts(status)


@router.post("/v1/admin/drafts/{draft_id}/approve")
def approve(draft_id: str) -> dict:
    return admin_services.decide_draft(draft_id, approve=True)


@router.post("/v1/admin/drafts/{draft_id}/reject")
def reject(draft_id: str) -> dict:
    return admin_services.decide_draft(draft_id, approve=False)


# ------------------------------------------------------------ API keys

ROLES = ("read-only", "read-draft", "read-send")


class KeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # Role is the normal input (read-only default). scopes is an optional
    # power-user override for a fine-grained key.
    role: Literal["read-only", "read-draft", "read-send"] = "read-only"
    scopes: list[str] | None = None
    allowlist: list[str] = []
    rate_per_min: int | None = Field(default=None, ge=1, le=60)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class KeyPatch(BaseModel):
    role: Literal["read-only", "read-draft", "read-send"] | None = None
    scopes: list[str] | None = None
    allowlist: list[str] | None = None
    rate_per_min: int | None = Field(default=None, ge=1, le=60)
    disabled: bool | None = None


@router.post("/v1/admin/keys")
def create_key(body: KeyBody) -> dict:
    return admin_services.create_key(
        body.name, role=body.role, allowlist=body.allowlist, scopes=body.scopes,
        rate_per_min=body.rate_per_min, expires_in_days=body.expires_in_days)


@router.get("/v1/admin/keys")
def list_keys() -> list[dict]:
    return admin_services.list_keys()


@router.patch("/v1/admin/keys/{key_id}")
def update_key(key_id: int, body: KeyPatch) -> dict:
    return admin_services.update_key(key_id, role=body.role, scopes=body.scopes,
                                     allowlist=body.allowlist,
                                     rate_per_min=body.rate_per_min,
                                     disabled=body.disabled)


@router.post("/v1/admin/keys/{key_id}/rotate")
def rotate_key(key_id: int) -> dict:
    return admin_services.rotate_key(key_id)


# ------------------------------------------------------------ audit + status

@router.get("/v1/admin/audit")
def read_audit(limit: int = 100, actor: str | None = None) -> list[dict]:
    return admin_services.read_audit(limit, actor)


@router.get("/v1/admin/status")
def wa_status() -> dict:
    return admin_services.wa_status()


@router.get("/v1/admin/qr")
def qr() -> Response:
    """Login QR proxied from the sidecar (409 once logged in)."""
    png = sidecar.qr_png()
    return Response(content=png, media_type="image/png")


# The admin console is a static shell: it asks for the admin token in the
# browser and calls the JSON endpoints above (which ARE token-guarded), so the
# page carries no data itself and needs no server-side auth to be served.
page_router = APIRouter()


def _console() -> HTMLResponse:
    return HTMLResponse((TEMPLATES / "approvals.html").read_text(encoding="utf-8"))


@page_router.get("/admin", include_in_schema=False)
def admin_console() -> HTMLResponse:
    return _console()


@page_router.get("/admin/approvals", include_in_schema=False)
def approvals_page() -> HTMLResponse:  # kept for older links/bookmarks
    return _console()
