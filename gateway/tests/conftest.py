"""Shared fixtures: fresh temp databases per test, a seeded message archive,
a scripted fake sidecar, and API-key helpers."""

import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

# Mirrors the sidecar's messages.db schema (sidecar/internal/store/store.go).
# If that schema changes, update this copy and the wastore queries together.
ARCHIVE_SCHEMA = """
CREATE TABLE chats (
    jid TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
    is_group INTEGER NOT NULL DEFAULT 0, last_message_ts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE contacts (
    jid TEXT PRIMARY KEY, push_name TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL DEFAULT '', business_name TEXT NOT NULL DEFAULT '');
CREATE TABLE messages (
    chat_jid TEXT NOT NULL, id TEXT NOT NULL, sender_jid TEXT NOT NULL,
    ts INTEGER NOT NULL, is_from_me INTEGER NOT NULL, kind TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '', media_ref TEXT, PRIMARY KEY (chat_jid, id));
"""

ALICE = "972501111111@s.whatsapp.net"
BOB = "972502222222@s.whatsapp.net"
GROUP = "120363000000000001@g.us"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Point the app at fresh per-test databases and reset cached state."""
    monkeypatch.setenv("GATEWAY_DB", str(tmp_path / "gateway.db"))
    monkeypatch.setenv("MESSAGES_DB", str(tmp_path / "messages.db"))
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("SIDECAR_TOKEN", "test-sidecar-token")
    monkeypatch.setenv("SIDECAR_URL", "http://sidecar.invalid:8081")
    # TestClient sends Host: testserver; the MCP transport must accept it.
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*,testserver")
    from app import policy
    from app.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setattr(policy, "rate_limiter", policy.RateLimiter())
    from app import db
    db.init()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def archive(env, tmp_path):
    """A message archive with two DMs and a group, as the sidecar would write."""
    conn = sqlite3.connect(os.environ["MESSAGES_DB"])
    conn.executescript(ARCHIVE_SCHEMA)
    conn.executemany("INSERT INTO chats VALUES (?, ?, ?, ?)", [
        (ALICE, "Alice", 0, 1000),
        (BOB, "Bob", 0, 3000),
        (GROUP, "Family", 1, 2000),
    ])
    conn.executemany("INSERT INTO contacts VALUES (?, ?, ?, ?)", [
        (ALICE, "Alice", "Alice Cohen", ""),
        (BOB, "Bob", "Bob Levi", ""),
    ])
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
        (ALICE, "A1", ALICE, 900, 0, "text", "hi, lunch tomorrow?", None),
        (ALICE, "A2", "me", 1000, 1, "text", "sure, 13:00", None),
        (BOB, "B1", BOB, 3000, 0, "image", "check this out",
         '{"media_type":"image","mime_type":"image/jpeg","direct_path":"/v/x",'
         '"media_key":"AQI=","file_sha256":"Aw==","file_enc_sha256":"BA==","file_length":9}'),
        (GROUP, "G1", ALICE, 2000, 0, "text", "who brings dessert to dinner?", None),
    ])
    conn.commit()
    conn.close()


@pytest.fixture()
def fake_sidecar(monkeypatch):
    """Replace the sidecar HTTP client with a scripted double; records calls."""
    from app.sidecar import SidecarError

    calls = {"send": []}

    def send_text(to, text):
        calls["send"].append((to, text))
        return {"message_id": f"MSG{len(calls['send'])}", "ts": 1700000000}

    def fail(status=503, msg="not logged in to WhatsApp"):
        def _f(*_a, **_kw):
            raise SidecarError(status, msg)
        return _f

    monkeypatch.setattr("app.sidecar.send_text", send_text)
    monkeypatch.setattr("app.sidecar.status",
                        lambda: {"connected": True, "logged_in": True,
                                 "jid": "me@s.whatsapp.net", "push_name": "Me",
                                 "waiting_for_qr": False})
    monkeypatch.setattr("app.sidecar.media", lambda c, m: (b"IMAGEBYTES", "image/jpeg"))
    calls["make_send_fail"] = lambda status=503: monkeypatch.setattr(
        "app.sidecar.send_text", fail(status))
    return calls


@pytest.fixture()
def client(env):
    from app.main import app
    return TestClient(app)


@pytest.fixture()
def make_key(env):
    """Create an API key directly (as the admin CLI would) and return its bearer."""
    from app import auth as auth_mod

    def _make(name="agent", scopes=None, role=None, allowlist=None, rate=6):
        if role is not None:
            return auth_mod.create_key(name, allowlist or [], rate, role=role)
        # Back-compat: scope-based creation (role inferred). Default = full.
        scopes = scopes if scopes is not None else list(auth_mod.ALL_SCOPES)
        return auth_mod.create_key(name, allowlist or [], rate, scopes=scopes)

    return _make


def bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def admin_headers() -> dict:
    return {"Authorization": "Bearer test-admin-token"}
