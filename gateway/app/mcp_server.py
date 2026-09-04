"""MCP interface: the same capabilities as REST, as tools for AI agents.

Authentication happens in an ASGI middleware wrapping the mounted MCP app:
it resolves the Bearer key and stashes the AuthContext in a contextvar the
tools read. Approve/reject deliberately do NOT exist here — approval is a
human act and only lives on the admin REST API.
"""

import functools
import json
from contextvars import ContextVar

import anyio.to_thread

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import auth, services
from .auth import AuthContext
from .config import get_settings

CURRENT_AUTH: ContextVar[AuthContext | None] = ContextVar("wagw_auth", default=None)

# stateless_http: single-user local server, no session resumption needed.
# streamable_http_path="/": the app is mounted at /mcp already; without this
# the endpoint would end up at /mcp/mcp.
mcp = FastMCP(
    "wa-gw",
    stateless_http=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[h.strip() for h in get_settings().mcp_allowed_hosts.split(",") if h.strip()],
    ),
)


def _auth() -> AuthContext:
    ctx = CURRENT_AUTH.get()
    if ctx is None:  # unreachable when mounted behind the middleware
        raise RuntimeError("no authentication context")
    return ctx


async def _run(fn, *args, **kwargs) -> str:
    """Run a sync service call on the threadpool and JSON-encode the result.

    Tool bodies block (httpx to the sidecar, sqlite); executed inline they
    would stall the single event loop for every other request. anyio copies
    the current contextvars into the worker thread, so _auth() state carries.
    """
    result = await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    return json.dumps(result)


@mcp.tool()
async def list_chats(query: str = "", limit: int = 20) -> str:
    """List recent WhatsApp chats (name, JID, last-activity time), most recent
    first. Optional query filters by chat name or JID substring."""
    return await _run(services.list_chats, _auth(), query=query, limit=limit)


@mcp.tool()
async def read_messages(chat_jid: str, limit: int = 30, before: int | None = None,
                        before_id: str = "") -> str:
    """Read messages from one chat, newest first. chat_jid comes from
    list_chats or search_contacts. To page back, pass before=<ts of the oldest
    message you have> and before_id=<its id> (the id makes the cursor exact
    when several messages share a timestamp)."""
    return await _run(services.list_messages, _auth(), chat_jid, limit=limit,
                      before=before, before_id=before_id or None)


@mcp.tool()
async def search_messages(query: str, chat_jid: str = "", limit: int = 20) -> str:
    """Full-text substring search across archived messages, optionally
    restricted to one chat."""
    return await _run(services.search_messages, _auth(), query, chat_jid or None, limit)


@mcp.tool()
async def check_new_messages(cursor: int | None = None, limit: int = 50) -> str:
    """Non-blocking check for new incoming messages. First call: omit cursor to
    get a starting point (returns {"cursor": N, "events": []} — no backlog).
    Later calls: pass the last cursor you got back; new messages since it are
    returned and the cursor advances. Returns immediately even when there is
    nothing new — check again between other actions rather than expecting a
    push. (Clients that can wait should use REST GET /v1/events?wait=N, which
    long-polls; a blocking MCP tool would trip host tool-call timeouts.)"""
    return await _run(services.list_events, _auth(), cursor, limit)


@mcp.tool()
async def search_contacts(query: str) -> str:
    """Find contacts by name or phone fragment; returns their JIDs for use as
    send/read targets."""
    return await _run(services.list_contacts, _auth(), query=query)


@mcp.tool()
async def send_message(to: str, text: str, send_at: int | None = None,
                       delay_seconds: int | None = None) -> str:
    """Send a WhatsApp message. 'to' is a JID or international phone number.
    Result status 'sent' means delivered to WhatsApp. Status 'pending_approval'
    is NORMAL, not an error: the recipient is outside this key's allowlist, so
    the message became a draft the human must approve; check it later with
    get_draft_status. To deliver LATER, pass send_at (unix ts) or delay_seconds
    (not both): status 'scheduled' means it will fire at that time — cancel it
    with cancel_draft until then."""
    return await _run(services.send_message, _auth(), to, text,
                      send_at=send_at, delay_seconds=delay_seconds)


@mcp.tool()
async def create_draft(to: str, text: str, note: str = "",
                       send_at: int | None = None,
                       delay_seconds: int | None = None) -> str:
    """Queue a message for explicit human approval (even for allowlisted
    recipients). Use note to tell the human why you wrote it. Pass send_at
    (unix ts) or delay_seconds to propose a delivery time — if approved, it
    sends at that time instead of immediately."""
    return await _run(services.create_draft, _auth(), to, text, note,
                      send_at=send_at, delay_seconds=delay_seconds)


@mcp.tool()
async def get_draft_status(draft_id: str) -> str:
    """Check one draft. Status is one of: pending, scheduled (approved, fires
    at send_at), sending (delivery in flight), sent, rejected, expired,
    canceled, or failed."""
    return await _run(services.get_draft, _auth(), draft_id)


@mcp.tool()
async def list_my_drafts() -> str:
    """List this key's drafts and their statuses, newest first."""
    return await _run(services.list_my_drafts, _auth())


@mcp.tool()
async def cancel_draft(draft_id: str) -> str:
    """Cancel one of this key's own drafts while it is still pending approval
    or scheduled (not yet delivered). Already-sent messages cannot be recalled."""
    return await _run(services.cancel_draft, _auth(), draft_id)


@mcp.tool()
async def request_permission(kind: str, contact: str = "", duration_hours: int = 0,
                             reason: str = "") -> str:
    """Ask the human for a scoped capability (they approve it live). kind is
    'send_recipient' (may auto-send to `contact`; add `duration_hours` to
    time-limit it) or 'send_window' (may auto-send to anyone for `duration_hours`).
    Returns a pending grant with an id; poll get_permission_status. This is how a
    read-only or read-draft key asks to be allowed to send."""
    return await _run(services.request_permission, _auth(), kind, contact or None,
                      duration_hours or None, reason)


@mcp.tool()
async def get_permission_status(grant_id: str) -> str:
    """Check one permission request: pending / approved / rejected / expired / revoked."""
    return await _run(services.get_permission_status, _auth(), grant_id)


@mcp.tool()
async def list_my_permissions() -> str:
    """List this key's permission requests/grants and their statuses, newest first."""
    return await _run(services.list_my_permissions, _auth())


@mcp.tool()
async def get_my_access() -> str:
    """What this key may do: role, rate limit, key expiry, send allowlist,
    active grants (with expiry), and — when restricted — the chats it may read.
    Call it at session start and re-check when a send is denied or a grant
    nears expiry, instead of probing by trial and error. Note: some chats may
    be hidden from you and are NOT listed here; a 404 chat may still exist."""
    return await _run(services.get_my_access, _auth())


class MCPAuthMiddleware:
    """ASGI wrapper for the mounted MCP app: Bearer key -> AuthContext contextvar."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin1").lower(): v.decode("latin1")
                   for k, v in scope.get("headers", [])}
        # OriginGuardMiddleware ran first and stamped the trusted client IP.
        client_ip = scope.get("state", {}).get("client_ip", "")
        # authenticate_bearer hits sqlite; keep that blocking call off the loop.
        ctx = await anyio.to_thread.run_sync(
            auth.authenticate_bearer, headers.get("authorization"), client_ip)
        if ctx is None:
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body",
                        "body": b'{"error": "missing or invalid API key"}'})
            return
        token = CURRENT_AUTH.set(ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            CURRENT_AUTH.reset(token)
