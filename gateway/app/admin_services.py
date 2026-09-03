"""Admin-only operations: approvals, key management, audit access, status.

These are invoked by the human (CLI or approvals page) with the ADMIN_TOKEN —
never by agents, and deliberately not exposed as MCP tools.
"""

import json
import time

from . import audit, auth, db, grants, policy, privacy, sidecar, wastore
from .auth import AuthContext
from .policy import PolicyError


def list_drafts(status: str | None = None, limit: int = 100) -> list[dict]:
    from .services import _sweep_expired
    _sweep_expired()
    sql = ("SELECT d.*, k.name AS key_name FROM drafts d"
           " JOIN api_keys k ON k.id = d.key_id")
    params: list = []
    if status:
        sql += " WHERE d.status = ?"
        params.append(status)
    sql += " ORDER BY d.created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _claim(draft_id: str, new_status: str, now: int, from_status: str = "pending") -> bool:
    """Atomically move a draft from from_status to new_status. The WHERE guard
    is the whole point: two concurrent decisions (console click vs Telegram tap,
    or a scheduler tick vs a cancel) can both observe the old status, but only
    one UPDATE wins — the loser sees rowcount 0. FastAPI runs sync endpoints on
    a threadpool, so this race is real even with a single uvicorn worker."""
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE drafts SET status = ?, decided_at = ? WHERE id = ? AND status = ?",
            (new_status, now, draft_id, from_status))
        return cur.rowcount == 1


def _conflict(draft_id: str, expected: str = "pending") -> PolicyError:
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        return PolicyError(404, "no such draft")
    return PolicyError(409, f"draft is {row['status']!r}, not {expected}")


def _fetch_draft_row(draft_id: str):
    with db.connect() as conn:
        return conn.execute(
            "SELECT d.*, k.name AS key_name, k.rate_per_min FROM drafts d"
            " JOIN api_keys k ON k.id = d.key_id WHERE d.id = ?",
            (draft_id,)).fetchone()


def _deliver_claimed(draft_id: str, row, release_status: str, actor: str) -> dict:
    """Deliver a draft already claimed into 'sending'. Retryable failures (rate
    limit, sidecar 503) release the row back to release_status — 'pending' when
    a human approved it, 'scheduled' when the scheduler fired it — so the send
    is retried later instead of lost."""
    now = int(time.time())

    def release(status: str) -> None:
        with db.connect() as conn:
            conn.execute("UPDATE drafts SET status = ?, decided_at = ? WHERE id = ?",
                         (status, now, draft_id))

    key_ctx = AuthContext(key_id=row["key_id"], name=row["key_name"],
                          rate_per_min=row["rate_per_min"])
    try:
        policy.enforce_rate_limits(key_ctx, actor)
    except PolicyError:
        release(release_status)  # rate limited now ≠ bad draft; retry later
        raise
    try:
        res = sidecar.send_text(row["to_jid"], row["body"])
    except sidecar.SidecarError as e:
        if e.status == 503:
            # Sidecar down or WhatsApp not linked: transient. Keep the draft
            # retryable instead of terminally failing it.
            release(release_status)
            audit.audit(actor, "send.deferred", resource=row["to_jid"],
                        detail={"draft_id": draft_id, "error": str(e)}, result="error")
        else:
            release("failed")
            audit.audit(actor, "send.failed", resource=row["to_jid"],
                        detail={"draft_id": draft_id, "error": str(e)}, result="error")
        raise
    with db.connect() as conn:
        conn.execute(
            "UPDATE drafts SET status = 'sent', decided_at = ?, sent_message_id = ? WHERE id = ?",
            (now, res.get("message_id"), draft_id))
    audit.audit(actor, "send.sent", resource=row["to_jid"],
                detail={"draft_id": draft_id, "on_behalf_of": row["key_name"],
                        "message_id": res.get("message_id")})
    return {"id": draft_id, "status": "sent", "message_id": res.get("message_id")}


def decide_draft(draft_id: str, approve: bool) -> dict:
    """Reject, or approve. Approving a draft with a future send_at parks it as
    'scheduled' (the scheduler delivers at time); otherwise it sends now.
    Delivery still counts against the key's per-minute rate and the global
    daily cap — a human clicking fast should not become a spam channel."""
    from .services import _sweep_expired
    _sweep_expired()
    now = int(time.time())

    if not approve:
        if not _claim(draft_id, "rejected", now):
            raise _conflict(draft_id)
        row = _fetch_draft_row(draft_id)
        audit.audit("admin", "draft.rejected", resource=draft_id,
                    detail={"key": row["key_name"], "to": row["to_jid"]})
        return {"id": draft_id, "status": "rejected"}

    # A scheduled draft is approved into 'scheduled', not sent: the human's
    # decision is captured now, the delivery happens at send_at.
    row = _fetch_draft_row(draft_id)
    if row is not None and row["send_at"] and row["send_at"] > now:
        if not _claim(draft_id, "scheduled", now):
            raise _conflict(draft_id)
        audit.audit("admin", "draft.scheduled", resource=row["to_jid"],
                    detail={"draft_id": draft_id, "send_at": row["send_at"]})
        return {"id": draft_id, "status": "scheduled", "send_at": row["send_at"]}

    # Claim first ('sending'), send second: a concurrent approval or a racing
    # cancel/reject can never double-send or be silently overridden.
    if not _claim(draft_id, "sending", now):
        raise _conflict(draft_id)
    return _deliver_claimed(draft_id, _fetch_draft_row(draft_id),
                            release_status="pending", actor="admin")


def cancel_scheduled(draft_id: str) -> dict:
    """Admin cancel of a not-yet-fired scheduled send."""
    if not _claim(draft_id, "canceled", int(time.time()), from_status="scheduled"):
        raise _conflict(draft_id, expected="scheduled")
    audit.audit("admin", "draft.canceled", resource=draft_id)
    return {"id": draft_id, "status": "canceled"}


# ---------------------------------------------------------------- permission grants

def list_grants(status: str | None = None, limit: int = 100) -> list[dict]:
    grants.sweep_expired()
    return grants.list_all(status, limit)


def _claim_grant(grant_id: str, new_status: str, now: int) -> bool:
    """Atomically move a pending grant to new_status (same concurrency primitive
    as drafts: a Telegram tap and a console click can't both win)."""
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE grants SET status = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
            (new_status, now, grant_id))
        return cur.rowcount == 1


def _grant_conflict(grant_id: str) -> PolicyError:
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM grants WHERE id = ?", (grant_id,)).fetchone()
    if row is None:
        return PolicyError(404, "no such permission request")
    return PolicyError(409, f"permission request is {row['status']!r}, not pending")


def decide_grant(grant_id: str, approve: bool) -> dict:
    """Approve (activate) or reject a pending grant. No sidecar send — a grant
    only authorizes future sends, which the policy engine then honors."""
    grants.sweep_expired()
    now = int(time.time())
    new_status = "approved" if approve else "rejected"
    if not _claim_grant(grant_id, new_status, now):
        raise _grant_conflict(grant_id)
    row = grants.get(grant_id)
    audit.audit("admin", "grant.approved" if approve else "grant.rejected",
                resource=grant_id,
                detail={"key": row["key_name"], "kind": row["kind"],
                        "to_jid": row["to_jid"], "expires_at": row["expires_at"]})
    return {"id": grant_id, "status": new_status}


def revoke_grant(grant_id: str) -> dict:
    """Revoke a previously approved (active) grant."""
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE grants SET status = 'revoked', decided_at = ? "
            "WHERE id = ? AND status = 'approved'",
            (int(time.time()), grant_id))
        if cur.rowcount == 0:
            raise PolicyError(404, "no active grant with that id")
    audit.audit("admin", "grant.revoked", resource=grant_id)
    return {"id": grant_id, "status": "revoked"}


# ---------------------------------------------------------------- chat privacy

def list_private_chats() -> list[dict]:
    return privacy.list_global()


def resolve_chats(q: str, limit: int = 20) -> list[dict]:
    """Search the archive's chats + contacts by name (or JID/phone fragment)
    for the admin picker. Display/selection only: what gets STORED and
    ENFORCED is always the pinned jid — a later rename cannot unhide a chat.
    Admin-only (this module sits behind require_admin), read-only queries."""
    q = (q or "").strip()
    if not q:
        return []
    limit = max(1, min(limit, 50))
    out, seen = [], set()
    for c in wastore.list_chats(q, limit):
        out.append({"jid": c["jid"], "name": c["name"],
                    "kind": "group" if c["is_group"] else "chat"})
        seen.add(c["jid"])
    for c in wastore.list_contacts(q, limit):
        if c["jid"] in seen:
            continue
        name = c["full_name"] or c["push_name"] or c["business_name"]
        out.append({"jid": c["jid"], "name": name, "kind": "contact"})
    return out[:limit]


def add_private_chat(jid: str, reason: str = "", name: str = "") -> dict:
    normalized = policy.normalize_jid(jid)
    privacy.add_global(normalized, reason, name.strip())
    audit.audit("admin", "privacy.chat_added", resource=normalized,
                detail={"reason": reason, "name": name.strip()})
    return {"jid": normalized, "name": name.strip(), "reason": reason}


def remove_private_chat(jid: str) -> dict:
    normalized = policy.normalize_jid(jid)
    if not privacy.remove_global(normalized):
        raise PolicyError(404, "that chat is not on the private list")
    audit.audit("admin", "privacy.chat_removed", resource=normalized)
    return {"jid": normalized, "removed": True}


def create_key(name: str, role: str = auth.ROLE_READ,
               allowlist: list[str] | None = None,
               scopes: list[str] | None = None,
               rate_per_min: int | None = None,
               expires_in_days: int | None = None,
               read_blocklist: list[str] | None = None,
               read_allowlist: list[str] | None = None) -> dict:
    """Create an agent API key with a role (read-only default, read-draft, or
    read-send). Allowlist entries are normalized so they match exactly what the
    policy engine compares at send time; for a read-send key the allowlist
    limits auto-send (off-list recipients fall to approval). Pass
    expires_in_days to make the key stop working after a set lifetime."""
    from .config import get_settings
    normalized = [policy.normalize_jid(a) for a in (allowlist or [])]
    read_block = [policy.normalize_jid(a) for a in (read_blocklist or [])]
    read_allow = [policy.normalize_jid(a) for a in (read_allowlist or [])]
    rate = rate_per_min or get_settings().default_rate_per_min
    expires_at = int(time.time()) + expires_in_days * 86400 if expires_in_days else None
    try:
        plaintext = auth.create_key(name, normalized, rate, role=role,
                                    scopes=scopes, expires_at=expires_at,
                                    read_blocklist=read_block,
                                    read_allowlist=read_allow)
    except ValueError as e:
        raise PolicyError(400, str(e))
    effective_role = role if scopes is None else auth.role_from_scopes(scopes)
    audit.audit("admin", "key.created", resource=name,
                detail={"role": effective_role, "allowlist": normalized,
                        "rate_per_min": rate, "expires_at": expires_at,
                        "read_blocklist": read_block, "read_allowlist": read_allow})
    return {"name": name, "key": plaintext, "role": effective_role,
            "expires_at": expires_at,
            "note": "store this key now — it is never shown again"}


def rotate_key(key_id: int) -> dict:
    """Issue a new secret for an existing key; the old secret keeps working for
    the configured grace window so agents can swap without a hard cutover."""
    from .config import get_settings
    grace = get_settings().key_rotation_grace_seconds
    try:
        plaintext = auth.rotate_key(key_id, grace)
    except KeyError:
        raise PolicyError(404, "no such key")
    audit.audit("admin", "key.rotated", resource=str(key_id),
                detail={"grace_seconds": grace})
    return {"id": key_id, "key": plaintext, "grace_seconds": grace,
            "note": "previous secret keeps working until the grace window ends"}


def list_keys() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, name, role, scopes, send_allowlist, rate_per_min, disabled,"
            " created_at, expires_at, last_used_at, last_used_ip,"
            " read_blocklist, read_allowlist,"
            " (prev_key_hash IS NOT NULL AND prev_expires_at > strftime('%s','now')) AS rotating"
            " FROM api_keys ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["scopes"] = json.loads(d["scopes"])
        d["send_allowlist"] = json.loads(d["send_allowlist"])
        d["read_blocklist"] = json.loads(d["read_blocklist"] or "[]")
        d["read_allowlist"] = json.loads(d["read_allowlist"] or "[]")
        d["rotating"] = bool(d["rotating"])
        d["role"] = d["role"] or auth.role_from_scopes(d["scopes"])
        out.append(d)
    return out


def update_key(key_id: int, role: str | None = None,
               scopes: list[str] | None = None,
               allowlist: list[str] | None = None,
               rate_per_min: int | None = None,
               disabled: bool | None = None,
               read_blocklist: list[str] | None = None,
               read_allowlist: list[str] | None = None) -> dict:
    sets, params, detail = [], [], {}
    if role is not None:
        # Changing role re-derives the scope set so the two never drift.
        try:
            role_scopes = auth.scopes_for_role(role)
        except ValueError as e:
            raise PolicyError(400, str(e))
        sets.append("role = ?")
        params.append(role)
        sets.append("scopes = ?")
        params.append(json.dumps(role_scopes))
        detail["role"] = role
    elif scopes is not None:
        unknown = [s for s in scopes if s not in auth.ALL_SCOPES]
        if unknown:
            raise PolicyError(400, f"unknown scopes: {unknown}")
        sets.append("scopes = ?")
        params.append(json.dumps(scopes))
        sets.append("role = ?")
        params.append(auth.role_from_scopes(scopes))
        detail["scopes"] = scopes
    if allowlist is not None:
        normalized = [policy.normalize_jid(a) for a in allowlist]
        sets.append("send_allowlist = ?")
        params.append(json.dumps(normalized))
        detail["allowlist"] = normalized
    if read_blocklist is not None:
        normalized = [policy.normalize_jid(a) for a in read_blocklist]
        sets.append("read_blocklist = ?")
        params.append(json.dumps(normalized))
        detail["read_blocklist"] = normalized
    if read_allowlist is not None:
        normalized = [policy.normalize_jid(a) for a in read_allowlist]
        sets.append("read_allowlist = ?")
        params.append(json.dumps(normalized))
        detail["read_allowlist"] = normalized
    if rate_per_min is not None:
        sets.append("rate_per_min = ?")
        params.append(rate_per_min)
        detail["rate_per_min"] = rate_per_min
    if disabled is not None:
        sets.append("disabled = ?")
        params.append(1 if disabled else 0)
        detail["disabled"] = disabled
    if not sets:
        raise PolicyError(400, "nothing to update")
    params.append(key_id)
    with db.connect() as conn:
        cur = conn.execute(f"UPDATE api_keys SET {', '.join(sets)} WHERE id = ?", params)
        if cur.rowcount == 0:
            raise PolicyError(404, "no such key")
    audit.audit("admin", "key.updated", resource=str(key_id), detail=detail)
    return {"id": key_id, "updated": detail}


def read_audit(limit: int = 100, actor: str | None = None) -> list[dict]:
    sql = "SELECT * FROM audit_log"
    params: list = []
    if actor:
        sql += " WHERE actor = ?"
        params.append(actor)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 1000)))
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def wa_status() -> dict:
    """Sidecar link status, shaped for the admin page / CLI."""
    try:
        return {"sidecar": "up", **sidecar.status()}
    except Exception as e:  # sidecar down ≠ gateway down; reads still work
        return {"sidecar": "down", "error": str(e)}
