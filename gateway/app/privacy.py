"""Read-side chat privacy: which chats a key may see.

Two layers, combined per request by visible_filter():
- a GLOBAL private list (private_chats table) no agent key can ever read, and
- PER-KEY lists on the key itself (read_blocklist hides chats for that key;
  a non-empty read_allowlist restricts it to exactly those chats).

This module decides WHAT to hide; wastore applies it INSIDE the SQL (so LIMIT
pagination stays correct and a hidden chat 404s without leaking existence).
Contacts are deliberately not filtered in v1 — they are the name→JID resolver
for sending, and hiding one silently breaks send targeting (see README).
"""

import time

from . import db
from .auth import AuthContext


def list_global() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT jid, reason, created_at FROM private_chats ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def add_global(jid: str, reason: str = "") -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO private_chats (jid, reason, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT(jid) DO UPDATE SET reason = excluded.reason",
            (jid, reason, int(time.time())),
        )


def remove_global(jid: str) -> bool:
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM private_chats WHERE jid = ?", (jid,))
        return cur.rowcount > 0


def visible_filter(auth: AuthContext) -> tuple[list[str], list[str] | None]:
    """The (deny, allow_only) pair every chat-scoped read threads into wastore.

    deny = global private list ∪ the key's read_blocklist (de-duped, order kept
    for stable SQL). allow_only = the key's read_allowlist, or None when empty
    (empty list on the key means "unrestricted", not "nothing").
    """
    deny = list(dict.fromkeys(
        [r["jid"] for r in list_global()] + list(auth.read_blocklist)))
    allow_only = list(auth.read_allowlist) or None
    return deny, allow_only
