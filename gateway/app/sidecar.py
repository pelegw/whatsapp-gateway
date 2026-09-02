"""HTTP client for the sidecar's internal API (the only path to WhatsApp actions)."""

import httpx

from .config import get_settings


class SidecarError(Exception):
    """Sidecar returned an error; .status carries the upstream HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _client() -> httpx.Client:
    s = get_settings()
    return httpx.Client(
        base_url=s.sidecar_url,
        headers={"X-Internal-Token": s.sidecar_token},
        timeout=30.0,
    )


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    """One sidecar call; network-level failures become SidecarError(503) so
    callers uniformly see 'temporarily unavailable' instead of a raw 500."""
    try:
        with _client() as c:
            resp = c.request(method, path, **kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        # Never reached the sidecar -> definitely not delivered -> retryable.
        raise SidecarError(503, f"sidecar unreachable: {e}") from e
    except httpx.HTTPError as e:
        # Request was already on the wire (e.g. read timeout): the send may have
        # gone through. Surface as 502 so a draft is NOT auto-returned to
        # pending — re-approving could double-send. The human investigates.
        raise SidecarError(502, f"sidecar request failed with unknown outcome: {e}") from e
    _raise_for(resp)
    return resp


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        try:
            msg = resp.json().get("error", resp.text)
        except ValueError:
            msg = resp.text
        raise SidecarError(resp.status_code, msg)


def status() -> dict:
    return _request("GET", "/status").json()


def qr_png() -> bytes:
    return _request("GET", "/qr").content


def send_text(to: str, text: str) -> dict:
    """Returns {"message_id": ..., "ts": ...} on success."""
    return _request("POST", "/send", json={"to": to, "text": text}).json()


def media(chat_jid: str, message_id: str) -> tuple[bytes, str]:
    """Returns (bytes, content_type)."""
    resp = _request("GET", "/media", params={"chat_jid": chat_jid, "message_id": message_id})
    return resp.content, resp.headers.get("content-type", "application/octet-stream")
