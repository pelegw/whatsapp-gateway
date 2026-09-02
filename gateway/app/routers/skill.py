"""GET /skill — public, unauthenticated agent guide.

Serves the agent-facing usage doc (Markdown) with the live base URL filled in,
so any agent can discover how to use this gateway just by fetching the URL. It
contains no secrets, so it needs no API key (like /v1/health)."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

router = APIRouter()
_GUIDE = Path(__file__).resolve().parent.parent / "templates" / "agent-guide.md"


def _base_url(request: Request) -> str:
    # Behind Caddy/Cloudflare the forwarded proto/host give the public URL;
    # fall back to the request URL for local runs.
    host = request.headers.get("host", request.url.netloc)
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{scheme}://{host}"


@router.get("/skill", include_in_schema=False)
@router.get("/skill.md", include_in_schema=False)
def skill(request: Request) -> PlainTextResponse:
    text = _GUIDE.read_text(encoding="utf-8").replace("{{BASE_URL}}", _base_url(request))
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")
