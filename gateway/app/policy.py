"""The send-policy engine: who may message whom, how, and how fast.

Decision for a send request, by the key's ROLE:
  - read-send  -> deliver now (auto-approved). If the key has an allowlist,
                  auto-send is limited to it and off-list recipients fall to a
                  draft; with no allowlist it may send to anyone.
  - read-draft -> always queue a draft for human approval.
  - read-only  -> denied.
Rate limits (per-key/minute token bucket + global daily cap) apply to every
actual delivery, including admin-approved drafts.
"""

import re
import threading
import time

from . import audit, grants
from .auth import ROLE_DRAFT, ROLE_SEND, AuthContext
from .config import get_settings

_PHONE_RE = re.compile(r"^\+?[0-9]{6,20}$")


class PolicyError(Exception):
    """A policy rejection with the HTTP status the API should return."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def normalize_jid(to: str) -> str:
    """Mirror the sidecar's recipient parsing so allowlists match exactly.

    Accepts full JIDs (user or group) or international phone numbers.
    """
    to = to.strip()
    if "@" in to:
        user, _, server = to.partition("@")
        # @lid is WhatsApp's hidden-user addressing; archived chats use it, so
        # it must be sendable. Strip both the ":N" device suffix and the ".N"
        # agent suffix, exactly as the sidecar's ToNonAD() does — otherwise the
        # allowlist/approved JID would differ from what actually gets delivered.
        # Legitimate user parts (phone numbers, group and lid ids) never contain
        # "." or ":", so this only ever removes those routing suffixes.
        user = user.split(":", 1)[0].split(".", 1)[0]
        if server not in ("s.whatsapp.net", "g.us", "lid") or not user:
            raise PolicyError(400, f"unsupported recipient {to!r} (want @s.whatsapp.net, @g.us, @lid, or a phone number)")
        return f"{user}@{server}"
    if not _PHONE_RE.match(to):
        raise PolicyError(400, f"recipient {to!r} is neither a JID nor an international phone number")
    return to.lstrip("+") + "@s.whatsapp.net"


class RateLimiter:
    """In-process per-key sliding-minute counter. Single uvicorn worker only —
    a second worker would get its own (uncoordinated) limiter."""

    def __init__(self):
        self._lock = threading.Lock()
        self._events: dict[int, list[float]] = {}

    def check(self, key_id: int, per_min: int) -> bool:
        """Record one send attempt; False when the key is over its budget."""
        now = time.monotonic()
        with self._lock:
            window = [t for t in self._events.get(key_id, []) if now - t < 60]
            if len(window) >= per_min:
                self._events[key_id] = window
                return False
            window.append(now)
            self._events[key_id] = window
            return True


rate_limiter = RateLimiter()


def enforce_rate_limits(auth: AuthContext, actor: str) -> None:
    """Raise PolicyError(429) if this delivery would bust a limit."""
    s = get_settings()
    if audit.sends_in_last_day() >= s.global_sends_per_day:
        audit.audit(actor, "send.rate_limited", detail={"limit": "global_daily"}, result="denied")
        raise PolicyError(429, f"global cap of {s.global_sends_per_day} sends/day reached")
    if not rate_limiter.check(auth.key_id, auth.rate_per_min):
        audit.audit(actor, "send.rate_limited", detail={"limit": "per_key_minute"}, result="denied")
        raise PolicyError(429, f"rate limit: {auth.rate_per_min} sends/minute for this key")


def route_send(auth: AuthContext, to_jid: str) -> str:
    """Return 'direct' (deliver now) or 'draft' (needs approval) for the key's
    role, else raise 403.

    read-send auto-delivers: to anyone if it has no allowlist, or to allowlisted
    recipients while routing off-list ones to a draft. read-draft always drafts.
    read-only cannot send. An active human-approved GRANT (recipient-scoped or a
    time window) widens this — a matching grant turns a would-be draft/denial
    into a direct send, even for a read-only key.
    """
    if auth.role == ROLE_SEND:
        if not auth.send_allowlist or to_jid in auth.send_allowlist:
            return "direct"
        if grants.has_active(auth.key_id, to_jid):
            return "direct"          # off-allowlist but explicitly granted
        return "draft"               # off its allowlist -> ask a human
    if auth.role == ROLE_DRAFT:
        if grants.has_active(auth.key_id, to_jid):
            return "direct"          # grant auto-sends what would be a draft
        return "draft"
    # read-only
    if grants.has_active(auth.key_id, to_jid):
        return "direct"              # grant elevates a read-only key for this target
    audit.audit(auth.name, "send.denied", resource=to_jid,
                detail={"reason": "read-only key may not send"},
                result="denied")
    raise PolicyError(403, "this key is read-only and cannot send or draft messages")
