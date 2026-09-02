"""Append-only audit trail. Every authenticated action writes exactly one row,
so the human can always reconstruct what an agent saw and did."""

import json
import time

from . import db


def audit(actor: str, action: str, resource: str = "",
          detail: dict | None = None, result: str = "ok") -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, resource, detail, result)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (int(time.time()), actor, action, resource,
             json.dumps(detail or {}), result),
        )


def sends_in_last_day() -> int:
    """Actual deliveries in the past 24h; this feeds the global cap."""
    cutoff = int(time.time()) - 86400
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'send.sent' AND ts >= ?",
            (cutoff,),
        ).fetchone()
    return row["n"]
