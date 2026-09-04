"""The service layer: every capability of the gateway, behind auth + policy.

Both the REST routers and the MCP tools are thin wrappers over these
functions, so policy and audit behavior can never diverge between the two
interfaces. All functions take the acting AuthContext first.
"""

import time
import uuid

from . import audit, db, grants, notify, policy, privacy, sidecar, wastore
from .auth import (DRAFTS_READ, READ_CHATS, READ_CONTACTS, READ_MEDIA,
                   READ_MESSAGES, SEND_DIRECT, SEND_DRAFT, AuthContext)
from .config import get_settings
from .policy import PolicyError


def _clamp(n: int, hi: int) -> int:
    """Clamp a caller-supplied limit into [1, hi] — SQLite treats LIMIT -1 as
    'unlimited', so negative input must never reach a query."""
    return max(1, min(n, hi))


def _require(auth: AuthContext, scope: str) -> None:
    if not auth.has(scope):
        audit.audit(auth.name, "authz.denied", detail={"scope": scope}, result="denied")
        raise PolicyError(403, f"this API key lacks the {scope!r} scope")


# ---------------------------------------------------------------- reads

def list_chats(auth: AuthContext, query: str = "", limit: int = 50, offset: int = 0) -> list[dict]:
    _require(auth, READ_CHATS)
    audit.audit(auth.name, "read.chats", detail={"query": query})
    deny, allow_only = privacy.visible_filter(auth)
    return wastore.list_chats(query, _clamp(limit, 200), max(0, offset), deny, allow_only)


def get_chat(auth: AuthContext, jid: str) -> dict:
    _require(auth, READ_CHATS)
    audit.audit(auth.name, "read.chat", resource=jid)
    deny, allow_only = privacy.visible_filter(auth)
    # A hidden chat is indistinguishable from a nonexistent one (same 404).
    chat = wastore.get_chat(jid, deny, allow_only)
    if chat is None:
        raise PolicyError(404, f"no chat {jid!r} in the archive")
    return chat


def list_messages(auth: AuthContext, chat_jid: str, limit: int = 50,
                  before: int | None = None, after: int | None = None,
                  before_id: str | None = None, after_id: str | None = None) -> list[dict]:
    _require(auth, READ_MESSAGES)
    audit.audit(auth.name, "read.messages", resource=chat_jid,
                detail={"limit": limit, "before": before, "after": after})
    deny, allow_only = privacy.visible_filter(auth)
    return wastore.list_messages(chat_jid, _clamp(limit, 200), before, after,
                                 before_id, after_id, deny, allow_only)


def search_messages(auth: AuthContext, query: str, chat_jid: str | None = None,
                    limit: int = 20) -> list[dict]:
    _require(auth, READ_MESSAGES)
    audit.audit(auth.name, "read.search", detail={"query": query, "chat_jid": chat_jid})
    deny, allow_only = privacy.visible_filter(auth)
    return wastore.search_messages(query, chat_jid, _clamp(limit, 100), deny, allow_only)


def list_contacts(auth: AuthContext, query: str = "", limit: int = 50) -> list[dict]:
    _require(auth, READ_CONTACTS)
    audit.audit(auth.name, "read.contacts", detail={"query": query})
    return wastore.list_contacts(query, _clamp(limit, 200))


def get_media(auth: AuthContext, chat_jid: str, message_id: str) -> tuple[bytes, str]:
    _require(auth, READ_MEDIA)
    # Visibility check BEFORE the sidecar call: a private chat's media must
    # never leave the sidecar, not merely be withheld from the response.
    deny, allow_only = privacy.visible_filter(auth)
    if not wastore.chat_is_visible(chat_jid, deny, allow_only):
        raise PolicyError(404, f"no chat {chat_jid!r} in the archive")
    audit.audit(auth.name, "read.media", resource=f"{chat_jid}/{message_id}")
    return sidecar.media(chat_jid, message_id)


def list_events(auth: AuthContext, cursor: int | None = None, limit: int = 100) -> dict:
    """New incoming messages since `cursor` (a rowid from a previous call).

    cursor=None bootstraps: returns the current top of the archive and no
    backlog, so an agent starts \"from now\". Privacy filtering (privacy.py)
    applies exactly as it does to list/search. Empty polls are deliberately
    not audited — a long-poll checks every second and would flood the log.
    """
    _require(auth, READ_MESSAGES)
    if cursor is None:
        return {"cursor": wastore.max_message_rowid(), "events": []}
    deny, allow_only = privacy.visible_filter(auth)
    cutoff = int(time.time()) - get_settings().events_freshness_seconds
    events, next_cursor = wastore.list_events(
        max(0, cursor), _clamp(limit, 200), cutoff, deny, allow_only)
    if events:
        audit.audit(auth.name, "read.events",
                    detail={"count": len(events), "from": cursor, "to": next_cursor})
    return {"cursor": next_cursor, "events": events}


# ---------------------------------------------------------------- sends

def send_message(auth: AuthContext, to: str, text: str,
                 send_at: int | None = None, delay_seconds: int | None = None) -> dict:
    """Policy-routed send by role: read-send delivers immediately, read-draft
    (or a read-send key messaging off its allowlist) queues a draft, read-only
    is rejected. A future send_at (or delay_seconds) schedules the delivery
    instead: the same policy decision applies, but the message fires later."""
    if not text:
        raise PolicyError(400, "text must not be empty")
    to_jid = policy.normalize_jid(to)
    route = policy.route_send(auth, to_jid)
    if route == "draft":
        note = ("(auto-routed: recipient not on this key's allowlist)"
                if auth.role == "read-send" else "(read-draft key: awaiting approval)")
        draft = create_draft(auth, to_jid, text, note=note,
                             send_at=send_at, delay_seconds=delay_seconds)
        audit.audit(auth.name, "send.routed_to_draft", resource=to_jid,
                    detail={"draft_id": draft["id"]})
        return {"status": "pending_approval", "draft_id": draft["id"],
                "detail": "this message needs human approval before it is sent"}

    # Defense in depth: role routing already decided "direct", but require the
    # underlying scope too — UNLESS an active grant authorizes this recipient (a
    # grant legitimately elevates a key that lacks send:direct). This re-check at
    # delivery time means a message still can't be sent without either the scope
    # or a matching grant, even if role and scopes ever drifted apart.
    if not auth.has(SEND_DIRECT):
        if not grants.has_active(auth.key_id, to_jid):
            _require(auth, SEND_DIRECT)   # raises 403 + audits authz.denied
        audit.audit(auth.name, "grant.exercised", resource=to_jid)
    resolved_at = policy.resolve_send_at(send_at, delay_seconds)
    if resolved_at is not None:
        return _create_scheduled_direct(auth, to_jid, text, resolved_at)
    policy.enforce_rate_limits(auth, auth.name)
    try:
        res = sidecar.send_text(to_jid, text)
    except sidecar.SidecarError as e:
        audit.audit(auth.name, "send.failed", resource=to_jid,
                    detail={"error": str(e)}, result="error")
        raise
    audit.audit(auth.name, "send.sent", resource=to_jid,
                detail={"message_id": res.get("message_id"), "chars": len(text)})
    return {"status": "sent", **res}


def create_draft(auth: AuthContext, to: str, text: str, note: str = "",
                 send_at: int | None = None, delay_seconds: int | None = None) -> dict:
    _require(auth, SEND_DRAFT)
    if not text:
        raise PolicyError(400, "text must not be empty")
    to_jid = policy.normalize_jid(to)
    resolved_at = policy.resolve_send_at(send_at, delay_seconds)
    now = int(time.time())
    # A scheduled draft must survive as 'scheduled' past the pending TTL, and
    # the row should outlive its fire time by a bit for post-hoc inspection.
    expires_at = now + get_settings().draft_ttl_hours * 3600
    if resolved_at is not None:
        expires_at = max(expires_at, resolved_at + 3600)
    draft = {
        "id": str(uuid.uuid4()),
        "key_id": auth.key_id,
        "to_jid": to_jid,
        "body": text,
        "note": note,
        "status": "pending",
        "created_at": now,
        "expires_at": expires_at,
        "send_at": resolved_at,
    }
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO drafts (id, key_id, to_jid, body, note, status, created_at,
                                   expires_at, send_at)
               VALUES (:id, :key_id, :to_jid, :body, :note, :status, :created_at,
                       :expires_at, :send_at)""",
            draft,
        )
    audit.audit(auth.name, "draft.created", resource=to_jid,
                detail={"draft_id": draft["id"], "send_at": resolved_at})
    # Live approval channel (Telegram etc.) — the single choke point for every
    # draft path (POST /v1/drafts, MCP create_draft, send_message's draft route).
    # Non-fatal: a notification failure never fails the draft.
    notify.notify_draft({**draft, "key_name": auth.name})
    return draft


def _create_scheduled_direct(auth: AuthContext, to_jid: str, text: str, send_at: int) -> dict:
    """A direct-route send with a future send_at: pre-approved, fires later.
    Stored as a draft row in status 'scheduled' so the scheduler, admin console,
    cancellation, and audit all reuse the draft machinery."""
    now = int(time.time())
    draft_id = str(uuid.uuid4())
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO drafts (id, key_id, to_jid, body, note, status, created_at,
                                   expires_at, decided_at, send_at)
               VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?)""",
            (draft_id, auth.key_id, to_jid, text,
             "(auto-approved: scheduled direct send)", now,
             max(now + get_settings().draft_ttl_hours * 3600, send_at + 3600),
             now, send_at),
        )
    audit.audit(auth.name, "send.scheduled", resource=to_jid,
                detail={"draft_id": draft_id, "send_at": send_at})
    return {"status": "scheduled", "draft_id": draft_id, "send_at": send_at}


def _sweep_expired() -> None:
    """Lazy expiry: run before any draft read/decision. Cheap single sweep."""
    now = int(time.time())
    with db.connect() as conn:
        conn.execute(
            "UPDATE drafts SET status = 'expired', decided_at = ? "
            "WHERE status = 'pending' AND expires_at < ?",
            (now, now),
        )
        # 'sending' is the approval claim; if the process died mid-send the
        # claim would dangle forever, so age it out as failed.
        conn.execute(
            "UPDATE drafts SET status = 'failed' "
            "WHERE status = 'sending' AND decided_at < ?",
            (now - 300,),
        )


def list_my_drafts(auth: AuthContext, limit: int = 50) -> list[dict]:
    _require(auth, DRAFTS_READ)
    _sweep_expired()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM drafts WHERE key_id = ? ORDER BY created_at DESC LIMIT ?",
            (auth.key_id, _clamp(limit, 200)),
        ).fetchall()
    audit.audit(auth.name, "draft.list")
    return [dict(r) for r in rows]


def get_draft(auth: AuthContext, draft_id: str) -> dict:
    _require(auth, DRAFTS_READ)
    _sweep_expired()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE id = ? AND key_id = ?",
            (draft_id, auth.key_id),
        ).fetchone()
    if row is None:
        raise PolicyError(404, "no such draft for this key")
    audit.audit(auth.name, "draft.get", resource=draft_id)
    return dict(row)


def cancel_draft(auth: AuthContext, draft_id: str) -> dict:
    # Ownership is the authorization here, not a scope: cancel only ever
    # PREVENTS a send, and a grant-elevated key (e.g. read-only) must be able
    # to undo its own scheduled send even though it lacks send:draft.
    with db.connect() as conn:
        # Scheduled sends are cancellable until they fire; the atomic status
        # guard means a cancel racing the scheduler's claim can never win late.
        cur = conn.execute(
            "UPDATE drafts SET status = 'canceled', decided_at = ? "
            "WHERE id = ? AND key_id = ? AND status IN ('pending', 'scheduled')",
            (int(time.time()), draft_id, auth.key_id),
        )
        if cur.rowcount == 0:
            raise PolicyError(404, "no pending or scheduled draft with that id for this key")
    audit.audit(auth.name, "draft.canceled", resource=draft_id)
    return {"id": draft_id, "status": "canceled"}


def get_my_access(auth: AuthContext) -> dict:
    """Self-introspection: the key's own effective capabilities.

    Discloses only what the key COULD discover empirically (role, its own
    allowlists, rate, expiry, active grants) — never its blind spots: the
    per-key read_blocklist and the global private list stay out, because
    listing them would reveal exactly which chats exist but are hidden,
    defeating "a hidden chat is indistinguishable from a nonexistent one".
    Not audited: agents are told to poll this, and self-introspection says
    nothing about the human's data.
    """
    grants.sweep_expired()
    with db.connect() as conn:
        row = conn.execute("SELECT expires_at FROM api_keys WHERE id = ?",
                           (auth.key_id,)).fetchone()
    active = [
        {"id": g["id"], "kind": g["kind"], "to_jid": g["to_jid"],
         "expires_at": g["expires_at"]}
        for g in grants.list_for_key(auth.key_id, 200)
        if g["status"] == "approved"
    ]
    out = {
        "name": auth.name,
        "role": auth.role,
        "rate_per_min": auth.rate_per_min,
        "key_expires_at": row["expires_at"] if row else None,
        "send_allowlist": list(auth.send_allowlist),
        "active_grants": active,
    }
    if auth.read_allowlist:
        # Only when set: it bounds what the key can DO (entries are chats it can
        # already see). An empty list means unrestricted, so it is omitted.
        out["read_allowlist"] = list(auth.read_allowlist)
    return out


# ---------------------------------------------------------------- permission grants

def request_permission(auth: AuthContext, kind: str, contact: str | None = None,
                       duration_hours: int | None = None, reason: str = "") -> dict:
    """Ask the human (via the live channel / console) for a scoped capability.

    Any authenticated key may ask — a read-only key must be able to request the
    ability to send. Kinds:
      - send_recipient: may auto-send to `contact` (optionally only for a window).
      - send_window: may auto-send to anyone for `duration_hours`.
    Approving the returned pending grant activates it (see admin_services.decide_grant).
    """
    if kind not in grants.KINDS:
        raise PolicyError(400, f"unknown kind {kind!r} (want one of {list(grants.KINDS)})")
    now = int(time.time())
    expires_at = None
    if duration_hours is not None:
        if duration_hours <= 0:
            raise PolicyError(400, "duration_hours must be positive")
        capped = min(duration_hours, get_settings().grant_max_hours)
        expires_at = now + capped * 3600

    if kind == grants.KIND_RECIPIENT:
        if not contact:
            raise PolicyError(400, "send_recipient requires 'contact'")
        to_jid = policy.normalize_jid(contact)
    else:  # send_window
        if duration_hours is None:
            raise PolicyError(400, "send_window requires 'duration_hours'")
        to_jid = None

    grant = grants.create(auth.key_id, kind, to_jid, expires_at, reason)
    audit.audit(auth.name, "permission.requested", resource=grant["id"],
                detail={"kind": kind, "to_jid": to_jid, "expires_at": expires_at})
    notify.notify_grant_request({**grant, "key_name": auth.name})   # non-fatal
    return grant


def get_permission_status(auth: AuthContext, grant_id: str) -> dict:
    grants.sweep_expired()
    grant = grants.get(grant_id)
    if grant is None or grant["key_id"] != auth.key_id:
        raise PolicyError(404, "no such permission request for this key")
    return grant


def list_my_permissions(auth: AuthContext, limit: int = 50) -> list[dict]:
    grants.sweep_expired()
    return grants.list_for_key(auth.key_id, _clamp(limit, 200))
