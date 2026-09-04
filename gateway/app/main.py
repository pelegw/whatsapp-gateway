"""Gateway entrypoint: FastAPI app + mounted MCP server.

Run with exactly ONE uvicorn worker: rate limiting is in-process and SQLite
writes assume a single writer per database.
"""

import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import db, scheduler
from .config import get_settings, validate_exposure
from .mcp_server import MCPAuthMiddleware, mcp
from .origin import OriginGuardMiddleware
from .policy import PolicyError
from .routers import (admin, chats, contacts, drafts, events, health, me,
                      media, messages, permissions, send, skill)
from .sidecar import SidecarError


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail closed at boot on unsafe internet-exposure configs (e.g. public mode
    # with the admin plane left on the token alone).
    validate_exposure(get_settings())
    db.init()
    # Telegram long-poll worker: runs iff a bot token is configured (enable +
    # linked chat are managed at runtime from the admin panel). Started here so
    # it shares the app event loop; cancelled cleanly on shutdown.
    poll_task = None
    if get_settings().telegram_bot_token:
        from .notify import telegram
        poll_task = asyncio.create_task(telegram.poll_loop())
    # Scheduled-send worker: unconditional (no external dependency to gate on).
    # Tests never run it — they call scheduler._tick() directly.
    scheduler_task = asyncio.create_task(scheduler.scheduler_loop())
    # The MCP session manager MUST run inside the parent app's lifespan,
    # otherwise /mcp requests die with "Task group is not initialized".
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        for task in (poll_task, scheduler_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


# In public mode the interactive API docs (which reveal the full surface) are
# turned off; anyone through the edge could otherwise read them unauthenticated.
_public = get_settings().public_mode()
api = FastAPI(
    title="WA_GW", version="0.1.0", lifespan=lifespan,
    docs_url=None if _public else "/docs",
    redoc_url=None if _public else "/redoc",
    openapi_url=None if _public else "/openapi.json",
)

for r in (health.router, chats.router, messages.router, contacts.router,
          media.router, send.router, drafts.router, permissions.router,
          events.router, me.router, skill.router, admin.router, admin.page_router):
    api.include_router(r)


class GatewayApp:
    """ASGI front door: /mcp goes to the MCP server, everything else (and the
    lifespan, which also drives the MCP session manager) goes to FastAPI.

    A plain Starlette Mount("/mcp") would 307-redirect bare "/mcp" to "/mcp/",
    and MCP clients do not reliably follow redirects on POST — hence this.
    """

    def __init__(self):
        self.api = api
        self.mcp_app = MCPAuthMiddleware(mcp.streamable_http_app())

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (
                scope["path"] == "/mcp" or scope["path"].startswith("/mcp/")):
            scope = {**scope, "path": scope["path"][len("/mcp"):] or "/"}
            await self.mcp_app(scope, receive, send)
            return
        await self.api(scope, receive, send)


# OriginGuard runs first for BOTH the API and MCP paths: it enforces the
# Cloudflare origin secret and stamps the trusted client IP into scope state.
app = OriginGuardMiddleware(GatewayApp())


@api.exception_handler(PolicyError)
async def policy_error(_: Request, exc: PolicyError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"error": str(exc)})


@api.exception_handler(SidecarError)
async def sidecar_error(_: Request, exc: SidecarError) -> JSONResponse:
    # 503 passes through (sidecar up but not linked); anything else is a 502.
    status = 503 if exc.status == 503 else 502
    return JSONResponse(status_code=status, content={"error": f"sidecar: {exc}"})
