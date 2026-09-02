"""Read-only access to the sidecar's messages.db archive.

Two guards keep this strictly read-only: the SQLite URI mode=ro, and
PRAGMA query_only as a belt-and-braces second layer. If the archive does
not exist yet (fresh install, sidecar still pairing) we return empty data
instead of erroring, so the gateway is usable immediately.
"""

import os
import sqlite3
from contextlib import contextmanager

from .config import get_settings


@contextmanager
def connect_ro():
    path = get_settings().messages_db
    if not os.path.exists(path):
        yield None
        return
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()


def list_chats(query: str = "", limit: int = 50, offset: int = 0) -> list[dict]:
    with connect_ro() as conn:
        if conn is None:
            return []
        rows = conn.execute(
            """SELECT c.jid, c.name, c.is_group, c.last_message_ts
               FROM chats c
               WHERE c.name LIKE '%' || ? || '%' OR c.jid LIKE '%' || ? || '%'
               ORDER BY c.last_message_ts DESC LIMIT ? OFFSET ?""",
            (query, query, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def get_chat(jid: str) -> dict | None:
    with connect_ro() as conn:
        if conn is None:
            return None
        row = conn.execute("SELECT * FROM chats WHERE jid = ?", (jid,)).fetchone()
        return dict(row) if row else None


def list_messages(chat_jid: str, limit: int = 50,
                  before: int | None = None, after: int | None = None,
                  before_id: str | None = None, after_id: str | None = None) -> list[dict]:
    """Messages for one chat, newest first, ordered by (ts, id) descending.

    before/after are unix-second cursors. Timestamps are second-granular, so a
    plain ts comparison would skip same-second neighbors of the last message the
    caller saw; passing that message's id as before_id / after_id makes the
    cursor exact on the (ts, id) keyset.
    """
    sql = "SELECT chat_jid, id, sender_jid, ts, is_from_me, kind, text, media_ref IS NOT NULL AS has_media FROM messages WHERE chat_jid = ?"
    params: list = [chat_jid]
    if before is not None:
        if before_id:
            sql += " AND (ts < ? OR (ts = ? AND id < ?))"
            params.extend([before, before, before_id])
        else:
            sql += " AND ts < ?"
            params.append(before)
    if after is not None:
        if after_id:
            sql += " AND (ts > ? OR (ts = ? AND id > ?))"
            params.extend([after, after, after_id])
        else:
            sql += " AND ts > ?"
            params.append(after)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(limit)
    with connect_ro() as conn:
        if conn is None:
            return []
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def search_messages(query: str, chat_jid: str | None = None, limit: int = 20) -> list[dict]:
    """Substring search across the archive (LIKE; FTS5 is a later upgrade)."""
    sql = ("SELECT chat_jid, id, sender_jid, ts, is_from_me, kind, text,"
           " media_ref IS NOT NULL AS has_media FROM messages"
           " WHERE text LIKE '%' || ? || '%'")
    params: list = [query]
    if chat_jid:
        sql += " AND chat_jid = ?"
        params.append(chat_jid)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with connect_ro() as conn:
        if conn is None:
            return []
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_contacts(query: str = "", limit: int = 50) -> list[dict]:
    with connect_ro() as conn:
        if conn is None:
            return []
        rows = conn.execute(
            """SELECT jid, push_name, full_name, business_name FROM contacts
               WHERE push_name LIKE '%' || ? || '%'
                  OR full_name LIKE '%' || ? || '%'
                  OR jid LIKE '%' || ? || '%'
               ORDER BY full_name, push_name LIMIT ?""",
            (query, query, query, limit),
        ).fetchall()
        return [dict(r) for r in rows]
