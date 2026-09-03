"""API-key authentication and the scope model.

Keys look like "wagw_<48 hex>"; only their sha256 lands in the database.
The AuthContext travels through every service call so policy and audit
always know who is acting.
"""

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field

from . import db

# The full scope vocabulary. Scopes are the internal, fine-grained capability
# checks; roles (below) are the user-facing abstraction that maps onto them.
READ_CHATS = "read:chats"
READ_MESSAGES = "read:messages"
READ_CONTACTS = "read:contacts"
READ_MEDIA = "read:media"
SEND_DIRECT = "send:direct"   # may deliver a message (auto-approved)
SEND_DRAFT = "send:draft"     # may queue drafts for human approval
DRAFTS_READ = "drafts:read"   # may read back the status of its own drafts
ALL_SCOPES = [READ_CHATS, READ_MESSAGES, READ_CONTACTS, READ_MEDIA,
              SEND_DIRECT, SEND_DRAFT, DRAFTS_READ]
READ_SCOPES = [READ_CHATS, READ_MESSAGES, READ_CONTACTS, READ_MEDIA]

# Roles — the three permission levels a key can hold. Read-only is the default:
# an agent has its own phone number and should only be able to READ the user's
# messages unless explicitly trusted to draft or send.
ROLE_READ = "read-only"    # read messages/chats/contacts/media; cannot write
ROLE_DRAFT = "read-draft"  # read + compose drafts the human must approve & send
ROLE_SEND = "read-send"    # read + send on the user's behalf, auto-approved
ROLES = [ROLE_READ, ROLE_DRAFT, ROLE_SEND]


def scopes_for_role(role: str) -> list[str]:
    """The internal scope set a role grants."""
    if role == ROLE_SEND:
        return READ_SCOPES + [SEND_DIRECT, SEND_DRAFT, DRAFTS_READ]
    if role == ROLE_DRAFT:
        return READ_SCOPES + [SEND_DRAFT, DRAFTS_READ]
    if role == ROLE_READ:
        return list(READ_SCOPES)
    raise ValueError(f"unknown role {role!r} (want one of {ROLES})")


def role_from_scopes(scopes: list[str]) -> str:
    """Infer the role for a key created via an explicit scope list (back-compat)."""
    if SEND_DIRECT in scopes:
        return ROLE_SEND
    if SEND_DRAFT in scopes:
        return ROLE_DRAFT
    return ROLE_READ


@dataclass
class AuthContext:
    key_id: int
    name: str
    scopes: list[str] = field(default_factory=list)
    send_allowlist: list[str] = field(default_factory=list)
    rate_per_min: int = 6
    role: str = ROLE_READ
    # Per-key read privacy (see privacy.py): chats this key may never read, and
    # (when non-empty) the only chats it may read.
    read_blocklist: list[str] = field(default_factory=list)
    read_allowlist: list[str] = field(default_factory=list)

    def has(self, scope: str) -> bool:
        return scope in self.scopes


def generate_key() -> tuple[str, str]:
    """Return (plaintext_key, sha256_hash). Plaintext is shown exactly once."""
    plaintext = "wagw_" + secrets.token_hex(24)
    return plaintext, hash_key(plaintext)


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


# Refresh last_used_at at most this often, so per-request auth stays a read in
# the common case rather than a write on every single call.
_LAST_USED_THROTTLE = 60


def authenticate_bearer(authorization: str | None, client_ip: str = "") -> AuthContext | None:
    """Resolve an Authorization header to an AuthContext, or None (= 401).

    Honors key expiry and the rotation grace window (a rotated key's previous
    secret keeps working until prev_expires_at), and records throttled
    last-used metadata for the mgmt view.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    if not token.startswith("wagw_"):
        return None
    now = int(time.time())
    token_hash = hash_key(token)
    with db.connect() as conn:
        # Match either the current secret, or the previous one while still in
        # its grace window. disabled kills both immediately.
        row = conn.execute(
            "SELECT * FROM api_keys WHERE disabled = 0 AND ("
            "  key_hash = ?"
            "  OR (prev_key_hash = ? AND prev_expires_at IS NOT NULL AND prev_expires_at > ?)"
            ")",
            (token_hash, token_hash, now),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] is not None and row["expires_at"] <= now:
            return None
        _touch_last_used(conn, row, now, client_ip)
    scopes = json.loads(row["scopes"])
    role = row["role"] if row["role"] else role_from_scopes(scopes)
    return AuthContext(
        key_id=row["id"],
        name=row["name"],
        scopes=scopes,
        send_allowlist=json.loads(row["send_allowlist"]),
        rate_per_min=row["rate_per_min"],
        role=role,
        read_blocklist=json.loads(row["read_blocklist"] or "[]"),
        read_allowlist=json.loads(row["read_allowlist"] or "[]"),
    )


def _touch_last_used(conn, row, now: int, client_ip: str) -> None:
    """Throttled update of last_used_at/last_used_ip (skips the write if the
    row was already touched within the throttle window and the IP is unchanged)."""
    last = row["last_used_at"] or 0
    if now - last < _LAST_USED_THROTTLE and (row["last_used_ip"] or "") == client_ip:
        return
    conn.execute("UPDATE api_keys SET last_used_at = ?, last_used_ip = ? WHERE id = ?",
                 (now, client_ip or None, row["id"]))


def create_key(name: str, allowlist: list[str], rate_per_min: int,
               role: str = ROLE_READ, scopes: list[str] | None = None,
               expires_at: int | None = None,
               read_blocklist: list[str] | None = None,
               read_allowlist: list[str] | None = None) -> str:
    """Insert a new API key; returns the plaintext (the only time it exists).

    A role is the normal input and derives the scope set. An explicit scopes
    list may be passed instead for fine-grained keys; the role is then inferred.
    """
    if scopes is None:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r} (want one of {ROLES})")
        scopes = scopes_for_role(role)
    else:
        unknown = [s for s in scopes if s not in ALL_SCOPES]
        if unknown:
            raise ValueError(f"unknown scopes: {unknown}")
        role = role_from_scopes(scopes)
    plaintext, key_hash = generate_key()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO api_keys (name, key_hash, scopes, send_allowlist, rate_per_min,"
            " created_at, expires_at, role, read_blocklist, read_allowlist)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, key_hash, json.dumps(scopes), json.dumps(allowlist),
             rate_per_min, int(time.time()), expires_at, role,
             json.dumps(read_blocklist or []), json.dumps(read_allowlist or [])),
        )
    return plaintext


def rotate_key(key_id: int, grace_seconds: int) -> str:
    """Issue a fresh secret for an existing key, keeping the old one valid for
    grace_seconds so in-flight agents can swap without downtime. Returns the
    new plaintext. Scopes, allowlist, expiry and identity are preserved."""
    now = int(time.time())
    new_plaintext, new_hash = generate_key()
    with db.connect() as conn:
        # One statement: prev_key_hash captures the CURRENT key_hash before it
        # is overwritten, so concurrent rotations can't lose an update.
        cur = conn.execute(
            "UPDATE api_keys SET prev_key_hash = key_hash, prev_expires_at = ?, "
            "key_hash = ? WHERE id = ?",
            (now + grace_seconds, new_hash, key_id),
        )
        if cur.rowcount == 0:
            raise KeyError("no such key")
    return new_plaintext
