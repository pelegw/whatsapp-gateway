"""Data access for `grants` — granular, human-approved capabilities that
supplement a key's base role (e.g. "always send to X", "send to anyone for N
hours").

Kept dependency-light (imports only `db`) so `policy` can import it without a
cycle. Recipient JIDs stored here are already normalized by the caller
(policy.normalize_jid), matching how the send allowlist is compared.
"""

import time
import uuid

from . import db

KIND_RECIPIENT = "send_recipient"   # to_jid set; may auto-send to that recipient
KIND_WINDOW = "send_window"         # to_jid NULL; may auto-send to anyone until expiry
KINDS = (KIND_RECIPIENT, KIND_WINDOW)


def create(key_id: int, kind: str, to_jid: str | None,
           expires_at: int | None, reason: str) -> dict:
    """Insert a pending grant; returns the row as a dict."""
    grant = {
        "id": str(uuid.uuid4()),
        "key_id": key_id,
        "kind": kind,
        "to_jid": to_jid,
        "expires_at": expires_at,
        "reason": reason,
        "status": "pending",
        "created_at": int(time.time()),
    }
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO grants (id, key_id, kind, to_jid, expires_at, reason, status, created_at)"
            " VALUES (:id, :key_id, :kind, :to_jid, :expires_at, :reason, :status, :created_at)",
            grant,
        )
    return grant


def get(grant_id: str) -> dict | None:
    """One grant joined with the requesting key's name."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT g.*, k.name AS key_name FROM grants g"
            " JOIN api_keys k ON k.id = g.key_id WHERE g.id = ?",
            (grant_id,),
        ).fetchone()
    return dict(row) if row else None


def active_grant_for(key_id: int, to_jid: str, now: int | None = None):
    """The most recent active grant that authorizes sending to to_jid, or None.

    Active = approved and not past expiry. A recipient grant must match to_jid;
    a window grant matches any recipient.
    """
    now = now if now is not None else int(time.time())
    with db.connect() as conn:
        return conn.execute(
            "SELECT * FROM grants WHERE key_id = ? AND status = 'approved'"
            " AND (expires_at IS NULL OR expires_at > ?)"
            " AND ((kind = 'send_recipient' AND to_jid = ?) OR kind = 'send_window')"
            " ORDER BY created_at DESC LIMIT 1",
            (key_id, now, to_jid),
        ).fetchone()


def has_active(key_id: int, to_jid: str, now: int | None = None) -> bool:
    return active_grant_for(key_id, to_jid, now) is not None


def list_for_key(key_id: int, limit: int = 50) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM grants WHERE key_id = ? ORDER BY created_at DESC LIMIT ?",
            (key_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def list_all(status: str | None = None, limit: int = 100) -> list[dict]:
    sql = ("SELECT g.*, k.name AS key_name FROM grants g"
           " JOIN api_keys k ON k.id = g.key_id")
    params: list = []
    if status:
        sql += " WHERE g.status = ?"
        params.append(status)
    sql += " ORDER BY g.created_at DESC LIMIT ?"
    params.append(limit)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def sweep_expired() -> None:
    """Flip approved grants past their expiry to 'expired' (honest reporting;
    routing already re-checks expiry, so this is cosmetic/for listings)."""
    now = int(time.time())
    with db.connect() as conn:
        conn.execute(
            "UPDATE grants SET status = 'expired' "
            "WHERE status = 'approved' AND expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        )
