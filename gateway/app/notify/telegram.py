"""Telegram provider + admin management + long-poll worker.

Design (see the plan): the bot TOKEN lives in env only; ENABLE and the linked
CHAT are runtime state in gateway.db, managed from /admin. The poll loop runs
whenever a token is present (that's what makes linking + button taps work); the
`enabled` flag only gates whether approval cards are pushed. Approving a tap
reuses admin_services.decide_draft / decide_grant verbatim (atomic claim), so a
Telegram tap and a web-console click can never double-act.

All HTTP is synchronous httpx (mirrors app/sidecar.py); the async poll loop
offloads blocking calls with anyio.to_thread (same idiom as mcp_server._run).
"""

import asyncio
import contextlib
import html
import secrets
import time

import anyio.to_thread
import httpx

from .. import admin_services, audit, db
from ..config import get_settings
from ..policy import PolicyError

# ---- in-process state (single uvicorn worker → one instance) ---------------
_link_code: str | None = None   # one-time code the operator must echo to link
_link_expires: float = 0.0      # link window deadline (monotonic-ish wall clock)
_loop_running = False
_bot_username: str | None = None
_LINK_TTL = 300                 # seconds a linking code stays valid


class TelegramError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---- config accessors ------------------------------------------------------

def _token() -> str:
    return get_settings().telegram_bot_token


def _chat_id() -> str:
    return db.get_config("telegram_chat_id", "") or ""


def _enabled() -> bool:
    return db.get_config("telegram_enabled", "0") == "1"


# ---- low-level HTTP (these four are what tests monkeypatch) -----------------

def _client(http_timeout: float) -> httpx.Client:
    return httpx.Client(base_url=f"https://api.telegram.org/bot{_token()}",
                        timeout=http_timeout)


def _api(method: str, _http_timeout: float = 15.0, **json) -> dict:
    # _http_timeout bounds the HTTP call itself: short for sends/notifications
    # (so a Telegram brown-out can't tie up the agent's request for ~35s), long
    # only for the getUpdates long-poll.
    if not _token():
        raise TelegramError(503, "no Telegram bot token configured")
    try:
        with _client(_http_timeout) as c:
            resp = c.post(f"/{method}", json=json)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise TelegramError(503, f"telegram unreachable: {e}") from e
    except httpx.HTTPError as e:
        raise TelegramError(502, f"telegram request failed: {e}") from e
    body = resp.json() if resp.content else {}
    if not body.get("ok"):
        raise TelegramError(resp.status_code, body.get("description", resp.text))
    return body.get("result", {})


def _api_send_message(text: str, keyboard: dict | None = None) -> dict:
    payload = {"chat_id": _chat_id(), "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = keyboard
    return _api("sendMessage", **payload)


def _answer_callback(cb_id: str, text: str = "") -> None:
    with contextlib.suppress(TelegramError):
        _api("answerCallbackQuery", callback_query_id=cb_id, text=text)


def _edit_message(message_id: int, text: str) -> None:
    with contextlib.suppress(TelegramError):
        _api("editMessageText", chat_id=_chat_id(), message_id=message_id,
             text=text, parse_mode="HTML")


def _get_updates(offset: int, timeout: int) -> list:
    return _api("getUpdates", _http_timeout=timeout + 10.0, offset=offset,
                timeout=timeout, allowed_updates=["message", "callback_query"])


def _get_me() -> str | None:
    global _bot_username
    if _bot_username:
        return _bot_username
    if not _token():
        return None
    with contextlib.suppress(TelegramError):
        _bot_username = _api("getMe").get("username")
    return _bot_username


# ---- message building ------------------------------------------------------

def _esc(s) -> str:
    return html.escape(str(s or ""))


def _keyboard(t: str, item_id: str) -> dict:
    # callback_data "{d|g}:{a|r}:{uuid}" — ~40 bytes, under Telegram's 64-byte cap.
    return {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"{t}:a:{item_id}"},
        {"text": "❌ Reject", "callback_data": f"{t}:r:{item_id}"},
    ]]}


def _draft_text(d: dict) -> str:
    note = f"\n📝 {_esc(d['note'])}" if d.get("note") else ""
    when = ""
    if d.get("send_at"):
        # UTC, explicitly labeled — the operator's phone may be in any zone.
        stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(d["send_at"]))
        when = f"\n🕒 Scheduled for {stamp}"
    return (f"🟡 <b>Approve WhatsApp message?</b>\n"
            f"Key: <code>{_esc(d.get('key_name'))}</code>\n"
            f"To: <code>{_esc(d['to_jid'])}</code>{when}\n\n{_esc(d['body'])[:3500]}{note}")


def _describe_grant(g: dict) -> str:
    # Show the real breadth + duration so the human approves knowingly.
    exp = g.get("expires_at")
    window = ""
    if exp:
        hrs = max(1, round((exp - g.get("created_at", exp)) / 3600))
        window = f" for {hrs}h"
    if g["kind"] == "send_recipient":
        if exp:
            return f"send to <code>{_esc(g['to_jid'])}</code>{window}"
        return f"<b>always</b> send to <code>{_esc(g['to_jid'])}</code>"
    return f"send to <b>ANYONE</b>{window}"


def _grant_text(g: dict) -> str:
    reason = f"\nReason: {_esc(g['reason'])}" if g.get("reason") else ""
    return (f"🔐 <b>Permission request</b>\n"
            f"Key: <code>{_esc(g.get('key_name'))}</code>\n"
            f"Wants to: {_describe_grant(g)}{reason}")


def _outcome_text(t: str, status: str) -> str:
    label = "Message" if t == "d" else "Permission"
    icon = {"sent": "✅", "approved": "✅", "rejected": "❌", "revoked": "❌",
            "scheduled": "🕒"}.get(status, "•")
    return f"{icon} <b>{label} {status}</b>"


# ---- Notifier interface (called from notify._fan_out) ----------------------

def notify_draft(draft: dict) -> None:
    _api_send_message(_draft_text(draft), _keyboard("d", draft["id"]))


def notify_grant_request(grant: dict) -> None:
    _api_send_message(_grant_text(grant), _keyboard("g", grant["id"]))


# ---- update handling -------------------------------------------------------

def _try_link(msg: dict) -> None:
    """Link the chat only if a valid, unexpired code is echoed FROM A PRIVATE
    CHAT. This binds linking to the admin who started it (they got the code over
    an admin-authenticated channel) — a stranger messaging the bot can't hijack
    the channel, and a group chat can't be linked."""
    global _link_code
    if not _link_code or time.time() > _link_expires:
        return
    if msg.get("chat", {}).get("type") != "private":
        return
    if _link_code not in (msg.get("text") or ""):
        return
    chat_id = str(msg["chat"]["id"])
    user_id = str(msg.get("from", {}).get("id", ""))
    db.set_config("telegram_chat_id", chat_id)
    db.set_config("telegram_user_id", user_id)   # only this user may approve
    _link_code = None
    audit.audit("admin", "telegram.linked", detail={"chat_id": chat_id, "user_id": user_id})
    with contextlib.suppress(TelegramError):
        _api_send_message("✅ This chat is now linked for WA_GW approvals.")


def _handle_update(u: dict) -> None:
    """Dispatch one Telegram update. Sync (offloaded to a thread by the loop)."""
    msg = u.get("message")
    if msg:
        _try_link(msg)
        return

    cq = u.get("callback_query")
    if not cq:
        return
    cb_id = cq["id"]

    # Disabling the channel is a real kill switch — no taps are honored while off.
    if not _enabled():
        _answer_callback(cb_id, "approvals are disabled")
        return

    chat_id = cq.get("message", {}).get("chat", {}).get("id")
    message_id = cq.get("message", {}).get("message_id")
    from_id = str(cq.get("from", {}).get("id", ""))
    data = cq.get("data", "")

    # Authorize by BOTH the linked chat AND the linked operator user id (so a
    # linked group can't let other members approve, and a hijacked chat can't).
    linked_user = db.get_config("telegram_user_id", "") or ""
    chat_ok = bool(_chat_id()) and str(chat_id) == _chat_id()
    user_ok = (not linked_user) or from_id == linked_user
    if not (chat_ok and user_ok):
        _answer_callback(cb_id, "not authorized")
        audit.audit("admin", "telegram.rejected_chat",
                    detail={"chat_id": chat_id, "from_id": from_id}, result="denied")
        return
    try:
        t, action, item_id = data.split(":", 2)
    except ValueError:
        _answer_callback(cb_id, "bad request")
        return
    approve = action == "a"
    try:
        if t == "d":
            res = admin_services.decide_draft(item_id, approve)
        elif t == "g":
            res = admin_services.decide_grant(item_id, approve)
        else:
            _answer_callback(cb_id, "unknown")
            return
        _answer_callback(cb_id, res["status"])
        if message_id is not None:
            _edit_message(message_id, _outcome_text(t, res["status"]))
    except PolicyError as e:
        # 404/409 = decided elsewhere (console/CLI) — race handled gracefully.
        note = "already handled" if e.status in (404, 409) else str(e)
        _answer_callback(cb_id, note)
        if message_id is not None:
            _edit_message(message_id, _outcome_text(t, "already handled"))


async def poll_loop() -> None:
    """Long-poll getUpdates and dispatch. Runs whenever a token is configured;
    survives transient errors with backoff; cancels cleanly on shutdown."""
    global _loop_running
    _loop_running = True
    # Resume from the persisted offset so a restart doesn't replay buffered taps.
    offset = int(db.get_config("telegram_offset", "0") or "0")
    backoff = 1
    with contextlib.suppress(Exception):
        await anyio.to_thread.run_sync(lambda: _api("deleteWebhook"))  # else getUpdates 409s
    try:
        while True:
            try:
                timeout = get_settings().telegram_poll_timeout
                updates = await anyio.to_thread.run_sync(_get_updates, offset, timeout)
                backoff = 1
                for u in updates:
                    offset = u["update_id"] + 1
                    await anyio.to_thread.run_sync(_handle_update, u)
                if updates:
                    await anyio.to_thread.run_sync(db.set_config, "telegram_offset", str(offset))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                audit.audit("system", "telegram.poll_error", detail={"error": str(e)},
                            result="error")
                await asyncio.sleep(min(backoff, 60))
                backoff *= 2
    finally:
        _loop_running = False


# ---- admin panel operations ------------------------------------------------

def status() -> dict:
    return {
        "token_present": bool(_token()),
        "enabled": _enabled(),
        "chat_linked": bool(_chat_id()),
        "chat_id": _chat_id() or None,
        "bot_username": _get_me(),
        "loop_running": _loop_running,
    }


def start_linking() -> dict:
    global _link_code, _link_expires
    if not _token():
        raise PolicyError(400, "set TELEGRAM_BOT_TOKEN and redeploy first")
    _link_code = secrets.token_hex(3)          # one-time 6-char code
    _link_expires = time.time() + _LINK_TTL
    username = _get_me()
    return {"linking": True, "bot_username": username, "code": _link_code,
            "instructions": (f"Within 5 minutes, from your PRIVATE Telegram chat send "
                             f"this to @{username}:\n/start {_link_code}")}


def set_enabled(enabled: bool) -> dict:
    db.set_config("telegram_enabled", "1" if enabled else "0")
    audit.audit("admin", "telegram.enabled" if enabled else "telegram.disabled")
    return status()


def send_test() -> dict:
    if not _chat_id():
        raise PolicyError(400, "link a chat first")
    _api_send_message("✅ WA_GW test message — approvals will arrive here.")
    return {"sent": True}


def unlink() -> dict:
    db.set_config("telegram_chat_id", "")
    db.set_config("telegram_user_id", "")
    db.set_config("telegram_enabled", "0")
    audit.audit("admin", "telegram.unlinked")
    return status()
