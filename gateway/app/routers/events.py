"""Events feed: GET /v1/events — cursor + optional long-poll for new messages.

The one async endpoint in the gateway, on purpose. Every other route is sync
`def` and runs on FastAPI's threadpool, which is fine for millisecond work but
would pin a thread for the entire `wait` here — a handful of long-polling
agents would starve the pool and stall every other route. So the wait lives on
the event loop (asyncio.sleep) and only each short DB check hops to a thread.
"""

import asyncio
import time

import anyio.to_thread
from fastapi import APIRouter, Depends

from .. import services
from ..auth import AuthContext
from ..config import get_settings
from ..deps import current_auth

router = APIRouter()


@router.get("/v1/events")
async def events(cursor: int | None = None, wait: int = 0, limit: int = 100,
                 auth: AuthContext = Depends(current_auth)) -> dict:
    """No cursor: bootstrap — returns the current cursor and no backlog.
    With a cursor: new messages since it; `wait` (seconds) long-polls, returning
    early the moment something visible arrives."""
    if cursor is None:
        return await anyio.to_thread.run_sync(services.list_events, auth, None, limit)
    s = get_settings()
    wait = max(0, min(wait, s.events_max_wait_seconds))
    deadline = time.monotonic() + wait
    while True:
        result = await anyio.to_thread.run_sync(services.list_events, auth, cursor, limit)
        if result["events"] or time.monotonic() >= deadline:
            return result
        await asyncio.sleep(s.events_poll_interval_seconds)
