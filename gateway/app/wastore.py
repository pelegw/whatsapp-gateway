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


def _visibility_clause(column: str, deny: list[str], allow_only: list[str] | None,
                       params: list) -> str:
    """SQL fragment (leading ' AND ...') enforcing chat privacy (privacy.py).

    Filtering happens INSIDE the query — post-filtering fetched rows would
    corrupt LIMIT/OFFSET pagination and leak counts. `column` is a trusted
    literal from this module, never caller input; values are parameterized.
    """
    clause = ""
    if deny:
        clause += f" AND {column} NOT IN ({','.join('?' for _ in deny)})"
        params.extend(deny)
    if allow_only is not None:
        if not allow_only:
            clause += " AND 0"          # explicit empty allowlist = see nothing
        else:
            clause += f" AND {column} IN ({','.join('?' for _ in allow_only)})"
            params.extend(allow_only)
    return clause


def list_chats(query: str = "", limit: int = 50, offset: int = 0,
               deny: list[str] = (), allow_only: list[str] | None = None) -> list[dict]:
    # The LIKE pair is parenthesized so the appended AND visibility clause
    # binds to the whole match, not just the jid arm (OR/AND precedence).
    sql = ("SELECT c.jid, c.name, c.is_group, c.last_message_ts FROM chats c"
           " WHERE (c.name LIKE '%' || ? || '%' OR c.jid LIKE '%' || ? || '%')")
    params: list = [query, query]
    sql += _visibility_clause("c.jid", deny, allow_only, params)
    sql += " ORDER BY c.last_message_ts DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with connect_ro() as conn:
        if conn is None:
            return []
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_chat(jid: str, deny: list[str] = (),
             allow_only: list[str] | None = None) -> dict | None:
    sql = "SELECT * FROM chats WHERE jid = ?"
    params: list = [jid]
    sql += _visibility_clause("jid", deny, allow_only, params)
    with connect_ro() as conn:
        if conn is None:
            return None
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def chat_is_visible(jid: str, deny: list[str] = (),
                    allow_only: list[str] | None = None) -> bool:
    """Existence + visibility check for paths that never SELECT the chat
    themselves (media downloads go straight to the sidecar otherwise)."""
    sql = "SELECT 1 FROM chats WHERE jid = ?"
    params: list = [jid]
    sql += _visibility_clause("jid", deny, allow_only, params)
    with connect_ro() as conn:
        if conn is None:
            return False
        return conn.execute(sql, params).fetchone() is not None


def list_messages(chat_jid: str, limit: int = 50,
                  before: int | None = None, after: int | None = None,
                  before_id: str | None = None, after_id: str | None = None,
                  deny: list[str] = (), allow_only: list[str] | None = None) -> list[dict]:
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
    sql += _visibility_clause("chat_jid", deny, allow_only, params)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(limit)
    with connect_ro() as conn:
        if conn is None:
            return []
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def search_messages(query: str, chat_jid: str | None = None, limit: int = 20,
                    deny: list[str] = (), allow_only: list[str] | None = None) -> list[dict]:
    """Substring search across the archive (LIKE; FTS5 is a later upgrade)."""
    sql = ("SELECT chat_jid, id, sender_jid, ts, is_from_me, kind, text,"
           " media_ref IS NOT NULL AS has_media FROM messages"
           " WHERE text LIKE '%' || ? || '%'")
    params: list = [query]
    if chat_jid:
        sql += " AND chat_jid = ?"
        params.append(chat_jid)
    sql += _visibility_clause("chat_jid", deny, allow_only, params)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with connect_ro() as conn:
        if conn is None:
            return []
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def max_message_rowid() -> int:
    """Current top of the archive — the bootstrap cursor for /v1/events."""
    with connect_ro() as conn:
        if conn is None:
            return 0
        return conn.execute("SELECT COALESCE(MAX(rowid), 0) AS m FROM messages").fetchone()["m"]


def list_events(cursor: int, limit: int, ts_cutoff: int,
                deny: list[str] = (), allow_only: list[str] | None = None) -> tuple[list[dict], int]:
    """New messages since `cursor` (a rowid), oldest first, privacy-filtered.

    rowid is insert order, which is what an events feed wants — but history
    sync re-ingests OLD messages with NEW rowids, so ts_cutoff keeps replayed
    backlog out of the feed. Cursor rule: when rows are delivered, advance only
    to the last delivered rowid (a chat un-hidden later is not skipped past
    undelivered rows); when nothing matches, jump to the current max so a dead
    filtered/stale backlog is never rescanned on every poll.
    """
    with connect_ro() as conn:
        if conn is None:
            return [], cursor
        max_row = conn.execute("SELECT COALESCE(MAX(rowid), 0) AS m FROM messages").fetchone()["m"]
        if max_row <= cursor:
            return [], cursor
        sql = ("SELECT rowid, chat_jid, id, sender_jid, ts, is_from_me, kind, text,"
               " media_ref IS NOT NULL AS has_media FROM messages"
               " WHERE rowid > ? AND rowid <= ? AND ts >= ?")
        params: list = [cursor, max_row, ts_cutoff]
        sql += _visibility_clause("chat_jid", deny, allow_only, params)
        sql += " ORDER BY rowid ASC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        return rows, (rows[-1]["rowid"] if rows else max_row)


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
