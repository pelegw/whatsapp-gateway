"""MCP surface: tool registry, auth middleware, and a live HTTP handshake."""

import asyncio
import json

from .conftest import ALICE, bearer

EXPECTED_TOOLS = {
    "list_chats", "read_messages", "search_messages", "search_contacts",
    "send_message", "create_draft", "get_draft_status", "list_my_drafts",
    "request_permission", "get_permission_status", "list_my_permissions",
}


def test_registered_tools_match_contract():
    from app.mcp_server import mcp
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS
    # Approval is a human act: no approve/reject tool may ever appear here.
    assert not any("approve" in n or "reject" in n for n in names)


def test_tools_run_under_contextvar_auth(env, archive, make_key):
    # Tools are async (blocking work goes to the threadpool) and must still see
    # the contextvar auth that the ASGI middleware sets.
    from app import auth as auth_mod
    from app import mcp_server

    key = make_key(scopes=["read:chats"])
    ctx = auth_mod.authenticate_bearer(f"Bearer {key}")
    token = mcp_server.CURRENT_AUTH.set(ctx)
    try:
        chats = json.loads(asyncio.run(mcp_server.list_chats(query="alice")))
        assert [c["jid"] for c in chats] == [ALICE]
    finally:
        mcp_server.CURRENT_AUTH.reset(token)


def test_all_tools_are_async():
    # A sync tool would run inline on the event loop and freeze the gateway
    # for the duration of a sidecar call — regression guard.
    import inspect
    from app import mcp_server
    for name in EXPECTED_TOOLS:
        assert inspect.iscoroutinefunction(getattr(mcp_server, name)), name


def test_mcp_http_requires_key(env):
    # No lifespan needed: the auth middleware rejects before the MCP app runs.
    # (Also matters: the SDK session manager can only .run() once per process,
    # so only the handshake test below may enter the app lifespan.)
    from fastapi.testclient import TestClient
    from app.main import app
    # follow_redirects=False pins that /mcp answers directly (no 307 to /mcp/),
    # since MCP clients do not reliably follow redirects on POST.
    r = TestClient(app, follow_redirects=False).post("/mcp", json={})
    assert r.status_code == 401


INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "pytest", "version": "0"}},
}
SSE_HEADERS = {"Accept": "application/json, text/event-stream"}


def test_mcp_http_initialize_handshake(env, make_key):
    from fastapi.testclient import TestClient
    from app.main import app

    key = make_key(scopes=["read:chats"])
    with TestClient(app) as client:  # context manager runs the app lifespan
        r = client.post("/mcp", json=INITIALIZE, headers={**bearer(key), **SSE_HEADERS})
        assert r.status_code == 200, r.text
        assert "wa-gw" in r.text  # serverInfo names this gateway
