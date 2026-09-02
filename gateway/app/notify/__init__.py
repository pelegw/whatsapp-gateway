"""Notification fan-out for approval requests.

A pluggable seam: today only Telegram, but `Notifier` (base.py) lets Discord or
others slot in later. Every notify call is NON-FATAL — a channel failure is
audited and swallowed so it can never break the agent's request or a draft
insert. Telegram is imported lazily and only used when it is fully configured
(env token present + enabled in the admin panel + a chat linked)."""

from .. import audit
from ..config import get_settings
from ..db import get_config


def _telegram_active() -> bool:
    """Telegram provider is live iff the token is set (env) AND it's enabled AND
    a chat is linked (both DB-managed from the admin panel)."""
    if not get_settings().telegram_bot_token:
        return False
    if get_config("telegram_enabled", "0") != "1":
        return False
    return bool(get_config("telegram_chat_id"))


def _providers() -> list:
    provs = []
    if _telegram_active():
        from . import telegram
        provs.append(telegram)
    return provs


def _fan_out(method: str, item: dict) -> None:
    for p in _providers():
        try:
            getattr(p, method)(item)
        except Exception as e:                      # a channel outage never breaks the caller
            audit.audit("system", "notify.failed", resource=str(item.get("id", "")),
                        detail={"provider": getattr(p, "__name__", "?"), "method": method,
                                "error": str(e)}, result="error")


def notify_draft(draft: dict) -> None:
    _fan_out("notify_draft", draft)


def notify_grant_request(grant: dict) -> None:
    _fan_out("notify_grant_request", grant)
