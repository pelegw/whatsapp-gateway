# WA_GW — a personal WhatsApp gateway for AI agents

Give AI assistants access to your WhatsApp **without giving them your phone or
the power to impersonate you**. WA_GW enrolls as a WhatsApp *linked device*
(like WhatsApp Web) and becomes the only holder of the session. Agents never
touch WhatsApp — they talk to this gateway's API, and the gateway enforces
policy:

- **Role-based keys** — each agent key is `read-only` (the default), `read-draft`,
  or `read-send`; only a read-send key can message people on your behalf.
- **Send allowlists** — restrict a read-send key to specific recipients (off-list
  messages fall back to approval).
- **Draft + approval queue** — a read-draft key's messages become drafts that
  *you* approve from a browser (works from your phone) before they're sent.
- **Rate limits** — per-key per-minute and a global daily cap, drafts included.
- **Full audit log** — every read and every send attempt, per agent.

Interfaces: **REST** and **MCP** (streamable HTTP), so Claude Code / Claude
Desktop and any HTTP client can use it.

## Architecture

```
                          docker compose
   ┌───────────────────────────────────────────────────────────┐
   │  ┌─────────────┐  internal network   ┌──────────────────┐ │
   │  │   sidecar   │  X-Internal-Token   │     gateway      │ │   127.0.0.1:8080
   │  │ Go+whatsmeow│◄────────────────────│ FastAPI + MCP    │◄┼──── agents (REST/MCP)
   │  │ (WhatsApp   │                     │ keys, allowlists,│ │      you (admin/approvals)
   │  │  session)   │─── messages.db ────►│ drafts, audit    │ │
   │  └─────────────┘    (WAL, read-only) └──────────────────┘ │
   │   volume: wa_data                     volume: gw_data     │
   └───────────────────────────────────────────────────────────┘
```

- `sidecar/` (Go, [whatsmeow](https://github.com/tulir/whatsmeow)) holds the
  WhatsApp session, archives messages/chats/contacts into SQLite, and exposes a
  minimal token-guarded internal API (`/send`, `/media`, `/status`, `/qr`).
  It is never published to the host.
- `gateway/` (Python, FastAPI + the official `mcp` SDK) is the only thing
  agents reach. It reads the archive read-only and calls the sidecar for
  actions, after policy checks.

## Quick start

Prereqs: Docker Desktop (WSL2 backend on Windows).

```bash
cp .env.example .env
# fill SIDECAR_TOKEN and ADMIN_TOKEN, e.g.:  openssl rand -hex 32
docker compose up -d --build
```

### 1. Link WhatsApp

Open <http://localhost:8080/admin>, paste your `ADMIN_TOKEN` when
prompted — the page shows a QR code. On your phone: WhatsApp → Settings →
Linked devices → Link a device → scan. (The QR is also printed to
`docker compose logs sidecar`.)

After linking, the phone pushes a batch of recent history; within a minute
`GET /v1/health` shows `"whatsapp_logged_in": true` and chats appear.

The linked device shows up in WhatsApp as **WA_GW** (set `DEVICE_NAME` in
`.env` to change it). The name is sent at pairing, so if you've already linked,
unlink the device and rescan to rename it.

### 2. Create an agent API key

The admin CLI needs Python 3.12+ and `httpx` (`pip install httpx`):

```bash
export ADMIN_TOKEN=...            # same value as in .env
python gateway/cli/wagw.py keys create --name claude --role read-draft
```

The key (`wagw_...`) is printed **once**. A key has one of three **roles**
(agents have their own phone number, so **read-only is the default**):

| Role | The agent can… |
|---|---|
| `read-only` *(default)* | read your chats, messages, contacts, media — **nothing else** |
| `read-draft` | also compose drafts; **you** approve and send them |
| `read-send` | also **send on your behalf, auto-approved** (no approval step) |

A `read-send` key with `--allow <recipient>` auto-sends **only** to those
recipients; messages to anyone off the list fall back to a draft you approve.
With no allowlist it can message anyone. Change a role later
(`PATCH /v1/admin/keys/{id}` `{"role":"..."}`), rotate the secret
(`wagw keys rotate <id>`), or revoke it (`wagw keys disable <id>`).

### 3. Connect Claude

```bash
claude mcp add --transport http wa-gw http://localhost:8080/mcp \
  --header "Authorization: Bearer wagw_..."
```

Tools exposed: `list_chats`, `read_messages`, `search_messages`,
`search_contacts`, `send_message`, `create_draft`, `get_draft_status`,
`list_my_drafts`, `request_permission`, `get_permission_status`,
`list_my_permissions`. There is deliberately **no approve tool** — approval is
human-only.

### Point agents at the gateway

Agents use the gateway two ways:

**MCP (Claude Code / Claude Desktop / any MCP client) — the main path.** Add the
server; its tools are self-describing, so the agent immediately knows the
capabilities:
```bash
claude mcp add --transport http wa-gw https://<your-host>/mcp   --header "Authorization: Bearer wagw_..."
```
Tools: `list_chats`, `read_messages`, `search_messages`, `search_contacts`,
`send_message`, `create_draft`, `get_draft_status`, `list_my_drafts`.

**A skill for good behavior (recommended).** `integrations/claude-skill/whatsapp-gateway/SKILL.md`
teaches the agent the policy model — that `pending_approval` is normal (not an
error), that a read-only key can't send, and that approval is human-only. Install
it by copying the folder into your skills directory:
```bash
cp -r integrations/claude-skill/whatsapp-gateway ~/.claude/skills/
```

**Other agents / plain HTTP.** Point them at the REST API table below with an
`Authorization: Bearer wagw_...` key. (The interactive `/docs` is disabled in
public mode, so the table here is the reference.)

**Self-describing guide.** The gateway serves the agent guide at **`GET /skill`**
(public, no key) with the live base URL filled in — hand an agent
`https://<your-host>/skill` and it can learn the endpoints, roles, and response
model on its own. Source: `gateway/app/templates/agent-guide.md`.

Give each agent its own key scoped to a **role** (`read-only` by default). The
gateway enforces the rest.

### 4. Approve drafts

When an agent messages someone outside its allowlist, the send returns
`pending_approval` and lands on <http://localhost:8080/admin> —
approve or reject from any browser on the machine. Draft statuses:
`pending → sending → sent`, or `rejected / canceled / expired` (24h TTL), plus
`failed` (delivery hit a hard error). `sending` is a brief in-flight state while
an approval is being delivered. Approving while WhatsApp is unlinked keeps the
draft `pending` so you can retry once it reconnects. Same thing via CLI:
`python gateway/cli/wagw.py approvals list / approve <id> / reject <id>`.

### 5. Approvals via Telegram (optional)

Approve or reject from your phone instead of the console. Create a bot with
**@BotFather**, then:

1. Put the token in `.env` as `TELEGRAM_BOT_TOKEN` and redeploy once (the token is
   the *only* Telegram secret; it never leaves env).
2. In `/admin` → **Telegram approvals**, click **Link my chat** — it shows a
   one-time code; from your **private** Telegram chat send `/start <code>` to your
   bot within 5 minutes. That links the chat *and* binds your Telegram user (only
   you can approve, even if the chat were a group). Then flip **Enable**.

Now every pending draft — and every privilege request (below) — arrives as a
Telegram card with **Approve / Reject** buttons. Tapping is equivalent to the web
console (an atomic claim means whichever you use first wins; the other becomes a
no-op). Connectivity is **long-polling** — no inbound webhook, no Cloudflare change.

### Privilege requests (grants)

An agent whose key can't send (read-only, or a recipient off its allowlist) can
**ask** for a scoped capability instead of failing: `POST /v1/permissions/request`
(or the MCP `request_permission` tool) with either
`{"kind":"send_recipient","contact":"+1…"}` ("always send to X", add
`duration_hours` to time-limit) or `{"kind":"send_window","duration_hours":2}`
("send to anyone for 2h"). You approve the **grant** in `/admin` (or Telegram);
an active grant *supplements* the key's role — so even a read-only key can be
granted send-to-X — and the policy engine honors it at send time. Manage grants
under `/v1/admin/grants` (list / approve / reject / revoke).

## REST API (agents)

`Authorization: Bearer wagw_...` on every call.

| Endpoint | Purpose |
|---|---|
| `GET /v1/chats?q=&limit=&offset=` | recent chats |
| `GET /v1/chats/{jid}/messages?limit=&before=&after=&before_id=&after_id=` | one chat's messages; page back with `before`+`before_id`, forward with `after`+`after_id` (id of the boundary message makes same-second cursors exact) |
| `GET /v1/messages/search?q=&chat_jid=` | search the archive |
| `GET /v1/contacts?q=` | contacts + JIDs |
| `GET /v1/media/{chat_jid}/{message_id}` | download media of an archived message |
| `POST /v1/send {to, text}` | 200 sent · 202 pending_approval · 403/429 denied |
| `POST /v1/drafts` / `GET /v1/drafts` / `DELETE /v1/drafts/{id}` | explicit drafts |
| `POST /v1/permissions/request` / `GET /v1/permissions[/{id}]` | request/track a scoped grant |
| `GET /v1/health` | no auth; gateway/sidecar/link status |

Admin endpoints (`Authorization: Bearer <ADMIN_TOKEN>`): `/v1/admin/keys`,
`/v1/admin/drafts` + `/approve|/reject`, `/v1/admin/grants` +
`/approve|/reject|/revoke`, `/v1/admin/telegram` (status/link/enable/test/unlink),
`/v1/admin/audit`, `/v1/admin/status`, `/v1/admin/qr`. Interactive docs at `/docs`.

## Runbooks

- **Re-login** (you unlinked it, or WhatsApp expired the device after ~2 weeks
  of the phone being offline): sends fail with 503 while reads keep working.
  The sidecar clears the dead session and restarts into a fresh QR
  automatically — just open the approvals page and rescan.
- **Logs**: `docker compose logs -f sidecar` (connection, QR) / `gateway`.
- **Backup**: the `wa_data` volume **is your WhatsApp session** (full account
  keys) plus message archive; `gw_data` holds API keys/drafts/audit. Back up
  only to encrypted storage; never commit them.
- **Moving to a NAS/VPS**: copy the repo + `.env`, `docker compose up -d`,
  re-link by QR. Keep 8080 loopback-only and reach it via SSH tunnel or
  Tailscale (`ports: "127.0.0.1:8080:8080"` is the guard). If you serve the
  MCP endpoint under another hostname, add it to `MCP_ALLOWED_HOSTS` — and note
  that a *portless* Host header (what `tailscale serve` sends over HTTPS/443, or
  a bare `http://host/mcp` on port 80) needs the **bare hostname** listed
  (`mybox.tailnet.ts.net`), not the `host:*` port-wildcard form.

## Exposing to the internet (Cloudflare)

By default the gateway is loopback-only. To let agents reach it from anywhere,
put it behind Cloudflare with the public overlay. The design keeps the human
**management plane** (approvals, key admin, QR login) behind Cloudflare Access
identity, while agents use API keys.

### Layers of protection

```
 agent (API key) ─┐
                  ├─▶ Cloudflare  ─(mTLS + secret header)─▶  Caddy :443  ─▶  gateway:8080
 you (SSO) ───────┘   • TLS, WAF, edge rate limiting            (origin)      • API keys / scopes
                      • Access SSO on /admin, /v1/admin          lockdown      • Access JWT check on admin
                      • Transform Rule injects X-WAGW-Origin                   • allowlist + approvals
```

1. **Cloudflare proxy** (orange cloud) terminates public TLS and fronts your origin.
2. **Origin lockdown** so nobody can bypass Cloudflare by hitting the origin IP:
   - **Authenticated Origin Pulls** — Caddy requires Cloudflare's client
     certificate (mTLS). The *global* origin-pull CA only proves the caller is
     some Cloudflare account, so for strong per-zone mTLS upload your **own**
     custom certificate (Cloudflare → SSL/TLS → Origin Server → Authenticated
     Origin Pulls, "per-hostname") and pin it in the Caddyfile. Otherwise the
     secret header below is your real per-deployment lock.
   - **Secret header** — a Cloudflare Transform Rule adds `X-WAGW-Origin: <ORIGIN_SECRET>`
     on every request; Caddy *and* the gateway reject anything without it.
3. **Cloudflare Access** on the admin paths — the origin verifies the signed
   Access JWT (`Cf-Access-Jwt-Assertion`) against your team's keys, so admin
   endpoints need a real logged-in identity **plus** the admin token. This is
   enforced by a **boot-time interlock**: if `ORIGIN_SECRET` is set (public
   mode) but Cloudflare Access is not enabled, the gateway refuses to start —
   admin is never silently left on the token alone. (Override only with
   `ALLOW_INSECURE_ADMIN=true`, at your own risk.)

### Setup

1. **DNS + proxy**: point `SITE_DOMAIN` (e.g. `wa.example.com`) at your origin in
   Cloudflare with the proxy enabled (orange cloud).
2. **Origin certs**: create a Cloudflare **Origin Certificate** and drop
   `origin.pem` / `origin.key` into `edge/certs/`, plus the
   `cloudflare-origin-pull-ca.pem` (see `edge/certs/README.md`). Turn on
   **Authenticated Origin Pulls** for the hostname.
3. **Origin secret**: set `ORIGIN_SECRET` in `.env` (`openssl rand -hex 32`), and
   add a Cloudflare **Transform Rule → Modify Request Header** that sets
   `X-WAGW-Origin` to that value on all requests to the hostname.
4. **Cloudflare Access**: create a **self-hosted Access application** covering
   `wa.example.com/admin*` and `wa.example.com/v1/admin*`, with a policy
   allowing your email. Copy the application **AUD** and your team domain into
   `.env` (`CF_ACCESS_ENABLED=true`, `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`,
   `CF_ACCESS_ALLOWED_EMAILS`).
5. **Edge rate limiting** (recommended): add a Cloudflare rate-limit rule on the
   hostname as a first line of defense — the gateway's per-key limits are the
   second.
6. **Launch** with the overlay:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.public.yml up -d --build
   ```
   The gateway no longer publishes a host port; only Caddy (`:443`) is exposed,
   and only to Cloudflare.

### Using it once public

- **Agents** call `https://wa.example.com/mcp` (or `/v1/...`) with their
  `Authorization: Bearer wagw_...` key. Add the public host to
  `MCP_ALLOWED_HOSTS` (bare hostname for HTTPS/443, see below).
- **You** open `https://wa.example.com/admin/approvals` — Cloudflare Access
  prompts for SSO, then the page works as usual.
- **The admin CLI** through Access uses a **service token**: create one in
  Cloudflare Access and export `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET`
  (plus `ADMIN_TOKEN` and `GATEWAY_URL=https://wa.example.com`) before running
  `wagw`.

### API key lifecycle

- **Expiry**: `wagw keys create ... --expires-in-days 90` — the key stops
  authenticating after that.
- **Rotation** (no downtime): `wagw keys rotate <id>` issues a new secret and
  keeps the old one valid for the grace window (`KEY_ROTATION_GRACE_SECONDS`,
  default 24h) so agents can swap over. `wagw keys list` shows `expires_at`,
  `last_used_at`, `last_used_ip`, and a `rotating` flag.
- **Revoke**: `wagw keys disable <id>` kills both current and previous secrets
  immediately.

## Development

```bash
cd sidecar && go test ./...          # store, API, extraction unit tests
cd gateway && python -m venv .venv && .venv/Scripts/pip install -e ".[dev]" \
  && .venv/Scripts/python -m pytest  # policy, auth, REST, drafts, MCP tests
```

Design notes worth knowing before you change things:

- The gateway runs **one** uvicorn worker on purpose: the rate limiter is
  in-process and SQLite has one writer per DB. Don't add `--workers 4`.
- `messages.db` is written **only** by the sidecar; the gateway opens it
  `mode=ro` (+ `PRAGMA query_only`). WAL makes concurrent reads safe. Both DBs
  live on named volumes because SQLite locking over NTFS bind mounts is
  unreliable under Docker Desktop.
- `normalize_jid` (gateway) and `ParseRecipient` (sidecar) must stay in
  agreement — allowlist checks compare against what the sidecar would actually
  send to.

## Caveats

- whatsmeow is an **unofficial** client; Meta's terms don't allow it and
  accounts (especially fresh, low-history ones) can get banned. The default
  policy posture — allowlists, human approval, conservative rate caps — exists
  partly to keep usage human-shaped. Use at your own risk.
- History backfill is whatever your phone ships at link time (typically recent
  messages), not your full archive.
