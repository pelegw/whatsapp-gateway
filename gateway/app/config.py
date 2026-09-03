"""Gateway configuration, environment-first (12-factor: everything via env vars)."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Where the sidecar lives on the internal Docker network.
    sidecar_url: str = "http://sidecar:8081"
    sidecar_token: str = ""

    # Single human/admin credential: approvals, key management, QR login.
    admin_token: str = ""

    # Sidecar-owned archive (we open it read-only) and our own database.
    messages_db: str = "/data/messages.db"
    gateway_db: str = "/gwdata/gateway.db"

    # Host headers accepted on the /mcp endpoint (DNS-rebinding protection).
    # Comma-separated; ":*" allows any port. Extend when serving beyond localhost.
    mcp_allowed_hosts: str = "localhost:*,127.0.0.1:*"

    # Policy defaults. Deliberately conservative: this is a personal account,
    # and slow, human-ish sending is also what keeps WhatsApp from flagging it.
    default_rate_per_min: int = 6      # per-key sends/minute
    global_sends_per_day: int = 100    # across all keys, approvals included
    draft_ttl_hours: int = 24          # pending drafts expire after this

    # --- Internet exposure (see README "Exposing to the internet") ---------

    # Origin lockdown. When origin_secret is set, EVERY request must carry it in
    # origin_secret_header (injected by a Cloudflare Transform Rule), else 403.
    # This stops anyone who finds the origin IP from bypassing Cloudflare.
    origin_secret: str = ""
    origin_secret_header: str = "x-wagw-origin"

    # Trust CF-Connecting-IP for the real client IP. Only honored once the
    # origin-secret check has passed, so a direct-to-origin caller can't spoof it.
    trust_cf_connecting_ip: bool = True

    # Cloudflare Access (Zero Trust) protects the admin/management plane. When
    # enabled, admin routes require BOTH a valid Access JWT (identity, verified
    # against the team's JWKS) AND the admin token (service credential).
    cf_access_enabled: bool = False
    cf_access_team_domain: str = ""   # e.g. myteam.cloudflareaccess.com
    cf_access_aud: str = ""           # the Access application's AUD tag
    cf_access_allowed_emails: str = ""  # comma-separated; empty = any authenticated identity

    # Grace window (seconds) during which a rotated key's PREVIOUS secret still
    # authenticates, so rotation needs no coordinated cutover. Default 24h.
    key_rotation_grace_seconds: int = 86400

    # Escape hatch: allow public mode (origin_secret set) WITHOUT Cloudflare
    # Access on the admin plane. Off by default so a misconfigured deploy fails
    # closed at boot instead of silently guarding admin with the token alone.
    allow_insecure_admin: bool = False

    # Telegram live-approval channel. The bot TOKEN is the only Telegram secret
    # and lives here (env only); whether it's enabled and which chat is linked are
    # runtime state managed from the admin panel (app_config table). With no token
    # the whole feature is inert (no poll loop, no notifications).
    telegram_bot_token: str = ""
    telegram_poll_timeout: int = 25    # getUpdates long-poll seconds
    grant_max_hours: int = 720         # cap on a requested grant's duration

    # Events feed (GET /v1/events). Freshness guards against history-sync
    # replays: old messages re-ingested with new rowids are not "new events".
    events_freshness_seconds: int = 300
    events_poll_interval_seconds: float = 1.0  # server-side long-poll check cadence
    events_max_wait_seconds: int = 30          # cap on the client's ?wait=

    # Scheduled sends ("approve now, deliver at T").
    scheduler_tick_seconds: int = 20           # how often due sends are fired
    schedule_max_horizon_days: int = 30        # furthest-out allowed send_at
    schedule_min_lead_seconds: int = 30        # send_at must be at least this far out

    def public_mode(self) -> bool:
        """True once an edge origin secret is configured (internet exposure)."""
        return bool(self.origin_secret)


def validate_exposure(s: "Settings") -> None:
    """Fail closed at startup on unsafe internet-exposure configurations."""
    if s.cf_access_enabled and (not s.cf_access_team_domain or not s.cf_access_aud):
        raise RuntimeError(
            "CF_ACCESS_ENABLED requires CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD")
    if s.public_mode() and not s.cf_access_enabled and not s.allow_insecure_admin:
        raise RuntimeError(
            "Public mode (ORIGIN_SECRET set) leaves the admin plane on the admin "
            "token alone. Enable Cloudflare Access (CF_ACCESS_ENABLED=true, "
            "CF_ACCESS_TEAM_DOMAIN, CF_ACCESS_AUD) to protect it, or set "
            "ALLOW_INSECURE_ADMIN=true to override at your own risk.")


@lru_cache
def get_settings() -> Settings:
    """Cached accessor; tests clear the cache after tweaking env vars."""
    return Settings()
