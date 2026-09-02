# Deploying WA_GW to EC2 behind Cloudflare

This puts the gateway on a public EC2 instance, reachable only through
Cloudflare, with the admin/management plane gated by Cloudflare Access SSO.

**Threat model recap.** The origin is locked down three ways so nobody who
learns the EC2 IP can bypass Cloudflare: (1) the **security group** only accepts
:443 from Cloudflare's IP ranges, (2) **Caddy** requires Cloudflare's client
certificate (Authenticated Origin Pulls), and (3) the app rejects any request
missing the **`X-WAGW-Origin` secret** that a Cloudflare Transform Rule injects.
Admin routes additionally require a **Cloudflare Access** identity. The gateway
**refuses to boot** in public mode if Access isn't configured.

---

## 0. Prerequisites

- An EC2 Linux instance (Amazon Linux 2023 or Ubuntu), **t3.small or larger**
  recommended (2 GB RAM builds the images comfortably; the provision script adds
  swap as a safety net).
- An **Elastic IP** associated with the instance (so the address is stable).
- A domain on Cloudflare; we'll use `wa.example.com` below — substitute yours.
- Your `.pem` SSH key, and the AWS CLI configured locally (for the security-group
  script; the console works too).

## 1. Provision the host

```bash
scp -i key.pem deploy/provision-ec2.sh ec2-user@<host>:~
ssh -i key.pem ec2-user@<host> 'bash provision-ec2.sh'
```
Log out and back in afterwards (for the `docker` group). This installs Docker +
Compose, adds 2 GB swap, and creates `/opt/wa-gw`.

## 2. Lock the security group to Cloudflare

Allow :443 **only** from Cloudflare, and :22 only from your IP:

```bash
SG_ID=sg-0abc123 SSH_CIDR=$(curl -s ifconfig.me)/32 deploy/update-security-group.sh
```
(Or in the console: inbound 443 from each range at <https://www.cloudflare.com/ips/>,
22 from your IP, remove any `0.0.0.0/0` on 443.)

## 3. Cloudflare — DNS, TLS, origin cert

1. **DNS**: add an `A` record `wa` → your Elastic IP, **Proxied** (orange cloud).
2. **SSL/TLS mode**: set to **Full (strict)**.
3. **Origin certificate**: SSL/TLS → Origin Server → *Create Certificate*. Save
   the cert and key to the host as:
   - `/opt/wa-gw/edge/certs/origin.pem`
   - `/opt/wa-gw/edge/certs/origin.key`
4. **Authenticated Origin Pulls**: SSL/TLS → Origin Server → enable it. Download
   Cloudflare's origin-pull CA and save it as
   `/opt/wa-gw/edge/certs/cloudflare-origin-pull-ca.pem`.
   (See `edge/certs/README.md`. For strong per-zone mTLS, upload your own custom
   AOP certificate instead and pin it in `edge/Caddyfile`.)

## 4. Cloudflare — origin secret (Transform Rule)

Generate a secret and add it as a request header on your hostname:

```bash
openssl rand -hex 32      # this is ORIGIN_SECRET
```
Rules → Transform Rules → **Modify Request Header** → *When incoming requests
match* `Hostname equals wa.example.com` → **Set static** `X-WAGW-Origin` = `<the secret>`.

## 5. Cloudflare — Access on the admin plane

1. Zero Trust → Access → **Applications** → *Add a self-hosted application*.
2. Application domains: `wa.example.com/admin*` **and** `wa.example.com/v1/admin*`.
3. Add a policy: *Allow* → your email(s).
4. From the app's settings copy the **Application Audience (AUD) tag** and your
   **team domain** (`yourteam.cloudflareaccess.com`).
5. *(Optional, recommended)* Security → WAF → **Rate limiting rule** on the
   hostname as an edge first line of defense.

## 6. Configure `.env` on the host

```bash
scp -i key.pem deploy/.env.production.example ec2-user@<host>:/opt/wa-gw/.env
ssh -i key.pem ec2-user@<host> 'nano /opt/wa-gw/.env'   # fill every value
```
Set `SIDECAR_TOKEN`, `ADMIN_TOKEN`, `ORIGIN_SECRET` (all `openssl rand -hex 32`),
`SITE_DOMAIN`, and the four `CF_ACCESS_*` values from step 5. Leave
`ALLOW_INSECURE_ADMIN` unset — the boot interlock is your safety net.

## 7. Deploy

From your laptop:
```bash
HOST=ec2-user@<host> SSH_KEY=key.pem deploy/push.sh
```
This syncs the code (never your secrets) and runs
`docker compose -f docker-compose.yml -f docker-compose.public.yml up -d --build`.
Only Caddy (:443) is exposed; the gateway stays on the internal network.

## 8. Link WhatsApp and create keys

1. Open **`https://wa.example.com/admin`** — Cloudflare Access prompts for SSO,
   then paste your `ADMIN_TOKEN`. The console shows the QR; scan it from
   WhatsApp → Linked devices. The device appears as **WA_GW**.
2. Create agent keys from the console (or CLI). Remember: **read-only** is the
   default; grant **read-draft** or **read-send** deliberately.
3. Point your agent at `https://wa.example.com/mcp` (or `/v1/...`) with its
   `Authorization: Bearer wagw_...` key. Add the bare hostname to
   `MCP_ALLOWED_HOSTS` if you serve MCP over 443 (see the README note).

## 9. Verify

```bash
curl https://wa.example.com/v1/health          # {"gateway":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' https://<elastic-ip>/v1/health   # should FAIL/timeout: origin not directly reachable
```
`https://wa.example.com/v1/admin/*` should require Access; a request without the
Cloudflare secret header (i.e. straight to the origin) should get 403.

## Operations

- **Update**: re-run `deploy/push.sh` — it rebuilds and restarts in place.
- **Logs**: `ssh … 'cd /opt/wa-gw && docker compose -f docker-compose.yml -f docker-compose.public.yml logs -f gateway'`.
- **Reboots**: `restart: unless-stopped` + `systemctl enable docker` (provision
  does this) bring the stack back automatically.
- **Backups**: the `wa_data` volume is your WhatsApp session + archive; `gw_data`
  holds keys/drafts/audit. Back both up to encrypted storage. **Never** commit
  `.env` or `edge/certs/*` (already gitignored).
- **Re-link**: if WhatsApp expires the device, reads keep working and sends 503;
  reopen `/admin` and rescan.
