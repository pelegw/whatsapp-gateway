"""Fires due scheduled sends (drafts in status 'scheduled' whose send_at has
arrived). Same lifecycle shape as notify/telegram.py's poll_loop: an asyncio
task started in the app lifespan, blocking work offloaded to a thread, clean
cancellation on shutdown.

Concurrency: each due row is claimed scheduled→sending atomically before
delivery (admin_services._claim), so an overlapping tick, a racing cancel, or a
second worker can never double-send. Retryable failures (rate limit, sidecar
503) release the row back to 'scheduled' inside _deliver_claimed, and the next
tick picks it up again — a burst scheduled for one instant drains at the key's
normal rate rather than being lost or blasted out.
"""

import asyncio
import contextlib
import time

import anyio.to_thread

from . import admin_services, audit, db, sidecar
from .config import get_settings
from .policy import PolicyError


def _tick() -> None:
    now = int(time.time())
    with db.connect() as conn:
        due = [r["id"] for r in conn.execute(
            "SELECT id FROM drafts WHERE status = 'scheduled' AND send_at <= ?"
            " ORDER BY send_at", (now,)).fetchall()]
    for draft_id in due:
        if not admin_services._claim(draft_id, "sending", now, from_status="scheduled"):
            continue  # lost the race: canceled, or another tick already took it
        row = admin_services._fetch_draft_row(draft_id)
        # Failures are already audited + the row released/parked inside
        # _deliver_claimed; one bad send must not stop the rest of the batch.
        with contextlib.suppress(PolicyError, sidecar.SidecarError):
            admin_services._deliver_claimed(draft_id, row,
                                            release_status="scheduled", actor="scheduler")


async def scheduler_loop() -> None:
    while True:
        try:
            await anyio.to_thread.run_sync(_tick)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            audit.audit("system", "scheduler.error", detail={"error": str(e)},
                        result="error")
        await asyncio.sleep(get_settings().scheduler_tick_seconds)
