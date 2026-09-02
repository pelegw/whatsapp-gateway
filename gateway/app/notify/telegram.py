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

import anyio.to_thread
import httpx

from .. import admin_services, audit, db
from ..config import get_settings
from ..policy import PolicyError

# ---- in-process state (single uvicorn worker → one instance) ---------------
_link_pending = False      # set by start_linking(); next inbound message links the chat
_loop_running = False
_bot_username: str | None = None


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

def _client() -> httpx.Client:
    return httpx.Client(base_url=f"https://api.telegram.org/bot{_token()}",
                        timeout=get_settings().telegram_poll_timeout + 10.0)


def _api(method: str, **json) -> dict:
    if not _token():
        raise TelegramError(503, "no Telegram bot token configured")
    try:
        with _client() as c:
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
    return _api("getUpdates", offset=offset, timeout=timeout,
                allowed_updates=["message", "callback_query"])


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
    return (f"🟡 <b>Approve WhatsApp message?</b>\n"
            f"Key: <code>{_esc(d.get('key_name'))}</code>\n"
            f"To: <code>{_esc(d['to_jid'])}</code>\n\n{_esc(d['body'])[:3500]}{note}")


def _describe_grant(g: dict) -> str:
    hrs = None
    if g.get("expires_at"):
        # approximate hours remaining from creation is not stored; show target/window
        hrs = "with a time limit"
    if g["kind"] == "send_recipient":
        base = f"always send to <code>{_esc(g['to_jid'])}</code>"
        return base + (" (time-limited)" if g.get("expires_at") else "")
    return "send to anyone (time-limited)"


def _grant_text(g: dict) -> str:
    reason = f"\nReason: {_esc(g['reason'])}" if g.get("reason") else ""
    return (f"🔐 <b>Permission request</b>\n"
            f"Key: <code>{_esc(g.get('key_name'))}</code>\n"
            f"Wants to: {_describe_grant(g)}{reason}")


def _outcome_text(t: str, status: str) -> str:
    label = "Message" if t == "d" else "Permission"
    icon = {"sent": "✅", "approved": "✅", "rejected": "❌", "revoked": "❌"}.get(status, "•")
    return f"{icon} <b>{label} {status}</b>"


# ---- Notifier interface (called from notify._fan_out) ----------------------

def notify_draft(draft: dict) -> None:
    _api_send_message(_draft_text(draft), _keyboard("d", draft["id"]))


def notify_grant_request(grant: dict) -> None:
    _api_send_message(_grant_text(grant), _keyboard("g", grant["id"]))


# ---- update handling -------------------------------------------------------

def _handle_update(u: dict) -> None:
    """Dispatch one Telegram update. Sync (offloaded to a thread by the loop)."""
    global _link_pending

    # (b) plain message — only used to link the chat.
    msg = u.get("message")
    if msg and _link_pending:
        db.set_config("telegram_chat_id", str(msg["chat"]["id"]))
        _link_pending = False
        audit.audit("admin", "telegram.linked", detail={"chat_id": msg["chat"]["id"]})
        with contextlib.suppress(TelegramError):
            _api_send_message("✅ This chat is now linked for WA_GW approvals.")
        return

    # (a) inline button tap.
    cq = u.get("callback_query")
    if not cq:
        return
    cb_id = cq["id"]
    chat_id = cq.get("message", {}).get("chat", {}).get("id")
    message_id = cq.get("message", {}).get("message_id")
    data = cq.get("data", "")

    if str(chat_id) != _chat_id() or not _chat_id():
        _answer_callback(cb_id, "not authorized")
        audit.audit("admin", "telegram.rejected_chat", detail={"chat_id": chat_id}, result="denied")
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
    offset, backoff = 0, 1
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
    global _link_pending
    if not _token():
        raise PolicyError(400, "set TELEGRAM_BOT_TOKEN and redeploy first")
    _link_pending = True
    username = _get_me()
    return {"linking": True, "bot_username": username,
            "instructions": (f"Open Telegram, send any message to @{username}, "
                             "then refresh — your chat will link automatically.")}


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
    db.set_config("telegram_enabled", "0")
    audit.audit("admin", "telegram.unlinked")
    return status()
