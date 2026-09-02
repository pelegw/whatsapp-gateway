"""gateway.db is the gateway's own state: API keys, drafts, audit log.

The message archive is NOT here; that is the sidecar's messages.db which we
only ever read (see wastore.py). Connections are cheap per-operation sqlite3
handles; a single uvicorn worker keeps write concurrency trivial.
"""

import sqlite3

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    key_hash       TEXT NOT NULL UNIQUE,      -- sha256 hex of the current key
    scopes         TEXT NOT NULL DEFAULT '[]',-- JSON array of scope strings
    send_allowlist TEXT NOT NULL DEFAULT '[]',-- JSON array of normalized JIDs
    rate_per_min   INTEGER NOT NULL DEFAULT 6,
    disabled       INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,
    expires_at     INTEGER,                   -- NULL = never expires
    prev_key_hash  TEXT,                      -- previous secret during rotation grace
    prev_expires_at INTEGER,                  -- when the previous secret stops working
    last_used_at   INTEGER,                   -- throttled: updated at most ~1/min
    last_used_ip   TEXT
);
CREATE TABLE IF NOT EXISTS drafts (
    id              TEXT PRIMARY KEY,          -- uuid4
    key_id          INTEGER NOT NULL,
    to_jid          TEXT NOT NULL,
    body            TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',  -- agent rationale, shown to the human
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    decided_at      INTEGER,
    sent_message_id TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY,
    ts       INTEGER NOT NULL,
    actor    TEXT NOT NULL,        -- API key name, or 'admin'
    action   TEXT NOT NULL,        -- e.g. read.chats, send.sent, send.denied
    resource TEXT NOT NULL DEFAULT '',
    detail   TEXT NOT NULL DEFAULT '',  -- JSON blob
    result   TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_settings().gateway_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


# Columns added after the first release; applied to pre-existing databases so
# an in-place upgrade doesn't require recreating gateway.db.
_MIGRATIONS = {
    "api_keys": {
        "expires_at": "INTEGER",
        "prev_key_hash": "TEXT",
        "prev_expires_at": "INTEGER",
        "last_used_at": "INTEGER",
        "last_used_ip": "TEXT",
        "role": "TEXT",   # read-only | read-draft | read-send; backfilled below
    },
}


def _migrate(conn) -> None:
    for table, columns in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    # Backfill role for keys created before roles existed, inferring it from the
    # stored scopes so their send behavior is unchanged.
    from .auth import role_from_scopes
    import json as _json
    for row in conn.execute("SELECT id, scopes FROM api_keys WHERE role IS NULL"):
        conn.execute("UPDATE api_keys SET role = ? WHERE id = ?",
                     (role_from_scopes(_json.loads(row["scopes"])), row["id"]))


def init() -> None:
    """Create tables if missing and add any new columns. Called at startup."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
