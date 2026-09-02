#!/usr/bin/env python3
"""wagw — admin CLI for the WhatsApp gateway.

Talks to the same admin REST API as the approvals page. Configuration via env:
  GATEWAY_URL  (default http://127.0.0.1:8080)
  ADMIN_TOKEN  (required)

Examples:
  wagw status
  wagw qr --out qr.png                       # save login QR, scan with phone
  wagw keys create --name reader             # read-only (the default)
  wagw keys create --name planner --role read-draft
  wagw keys create --name butler --role read-send   # sends on your behalf
  wagw keys create --name butler --role read-send \
      --allow +972501234567                  # auto-send only to this contact
  wagw keys rotate 3
  wagw approvals list
  wagw approvals approve <draft-id>
  wagw audit --limit 20
"""

import argparse
import json
import os
import sys

import httpx


def client() -> httpx.Client:
    token = os.environ.get("ADMIN_TOKEN")
    if not token:
        sys.exit("set ADMIN_TOKEN in the environment (same value as in .env)")
    headers = {"Authorization": f"Bearer {token}"}
    # When the admin plane is behind Cloudflare Access, a service token lets the
    # CLI through non-interactively: Cloudflare validates these and injects the
    # identity JWT the gateway verifies. Harmless when Access is not in use.
    cf_id = os.environ.get("CF_ACCESS_CLIENT_ID")
    cf_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET")
    if cf_id and cf_secret:
        headers["CF-Access-Client-Id"] = cf_id
        headers["CF-Access-Client-Secret"] = cf_secret
    return httpx.Client(
        base_url=os.environ.get("GATEWAY_URL", "http://127.0.0.1:8080"),
        headers=headers,
        timeout=30,
    )


def show(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def die_on_error(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        try:
            sys.exit(f"error {resp.status_code}: {resp.json().get('error', resp.text)}")
        except ValueError:
            sys.exit(f"error {resp.status_code}: {resp.text}")


def main() -> None:
    p = argparse.ArgumentParser(prog="wagw", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="WhatsApp link status")

    qr = sub.add_parser("qr", help="download the login QR PNG")
    qr.add_argument("--out", default="qr.png")

    keys = sub.add_parser("keys", help="manage agent API keys").add_subparsers(
        dest="keys_cmd", required=True)
    kc = keys.add_parser("create")
    kc.add_argument("--name", required=True)
    kc.add_argument("--role", choices=["read-only", "read-draft", "read-send"],
                    default="read-only",
                    help="read-only (default): read messages; read-draft: also "
                         "compose drafts you approve; read-send: also send on "
                         "your behalf (auto-approved)")
    kc.add_argument("--allow", action="append", default=[],
                    help="restrict a read-send key's auto-send to these recipients "
                         "(repeatable phone/JID); off-list messages become drafts")
    kc.add_argument("--rate", type=int, default=None, help="sends per minute")
    kc.add_argument("--expires-in-days", type=int, default=None,
                    help="key stops working after this many days")
    keys.add_parser("list")
    kd = keys.add_parser("disable")
    kd.add_argument("id", type=int)
    kr = keys.add_parser("rotate")
    kr.add_argument("id", type=int)

    ap = sub.add_parser("approvals", help="review pending drafts").add_subparsers(
        dest="approvals_cmd", required=True)
    ap.add_parser("list")
    aa = ap.add_parser("approve")
    aa.add_argument("draft_id")
    ar = ap.add_parser("reject")
    ar.add_argument("draft_id")

    au = sub.add_parser("audit", help="recent audit log entries")
    au.add_argument("--limit", type=int, default=50)
    au.add_argument("--actor", default=None)

    args = p.parse_args()
    with client() as c:
        if args.cmd == "status":
            r = c.get("/v1/admin/status")
        elif args.cmd == "qr":
            r = c.get("/v1/admin/qr")
            die_on_error(r)
            with open(args.out, "wb") as f:
                f.write(r.content)
            print(f"QR saved to {args.out} — scan via WhatsApp > Linked devices")
            return
        elif args.cmd == "keys" and args.keys_cmd == "create":
            r = c.post("/v1/admin/keys", json={
                "name": args.name,
                "role": args.role,
                "allowlist": args.allow,
                "rate_per_min": args.rate,
                "expires_in_days": args.expires_in_days,
            })
        elif args.cmd == "keys" and args.keys_cmd == "list":
            r = c.get("/v1/admin/keys")
        elif args.cmd == "keys" and args.keys_cmd == "disable":
            r = c.patch(f"/v1/admin/keys/{args.id}", json={"disabled": True})
        elif args.cmd == "keys" and args.keys_cmd == "rotate":
            r = c.post(f"/v1/admin/keys/{args.id}/rotate")
        elif args.cmd == "approvals" and args.approvals_cmd == "list":
            r = c.get("/v1/admin/drafts", params={"status": "pending"})
        elif args.cmd == "approvals":
            action = "approve" if args.approvals_cmd == "approve" else "reject"
            r = c.post(f"/v1/admin/drafts/{args.draft_id}/{action}")
        elif args.cmd == "audit":
            params = {"limit": args.limit}
            if args.actor:
                params["actor"] = args.actor
            r = c.get("/v1/admin/audit", params=params)
        else:  # unreachable
            p.error("unknown command")
        die_on_error(r)
        show(r.json())


if __name__ == "__main__":
    main()
