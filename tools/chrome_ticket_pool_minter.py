#!/usr/bin/env python3
"""Batch mint Chrome tickets into Panda pool (one browser, sequential accounts).

Uses statsig_meta as durable asset; does not rely on short-lived statsig in pool.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / ".tmp"
DEFAULT_SSO = TMP / "web-sso-canary.json"
PANDA_POOL = "/opt/grok2api/tools/panda_ticket_pool.py"
SSH_HOST = os.environ.get("PANDA_SSH", "panda")
GROK2API_BASE = os.environ.get("GROK2API_BASE", "").rstrip("/")
GROK2API_ADMIN_TOKEN = os.environ.get("GROK2API_ADMIN_TOKEN", "")


def log(event: str, **kw) -> None:
    row = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **kw}
    print(json.dumps(row, ensure_ascii=False), flush=True)


def load_modules():
    spec_c = importlib.util.spec_from_file_location("chrome", ROOT / "tools" / "_chrome_channel_chat_image.py")
    chrome = importlib.util.module_from_spec(spec_c)
    assert spec_c and spec_c.loader
    spec_c.loader.exec_module(chrome)
    spec_v = importlib.util.spec_from_file_location("v1", ROOT / "tools" / "web_http_chat_image_canary.v1.py")
    v1 = importlib.util.module_from_spec(spec_v)
    assert spec_v and spec_v.loader
    spec_v.loader.exec_module(v1)
    return chrome, v1


def load_accounts(path: Path) -> dict[int, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("accounts") if isinstance(data, dict) else data
    return {int(a["id"]): a for a in rows if a.get("sso")}


def sanitize_cookie(cookie: str) -> str:
    keep: list[str] = []
    for part in (cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name = part.split("=", 1)[0].strip().lower()
        if name in ("sso", "sso-rw", "cf_clearance", "__cf_bm"):
            continue
        keep.append(part)
    return "; ".join(keep)


def push_to_panda_go_api_via_ssh(pool_row: dict, *, ssh_host: str, ttl_hours: float) -> None:
    payload = {
        "account_id": pool_row["account_id"],
        "statsig_meta": pool_row.get("statsig_meta") or "",
        "cookie": pool_row.get("cookie") or "",
        "user_agent": pool_row.get("user_agent") or "",
        "sign_source": pool_row.get("sign_source") or "",
        "ttl_hours": ttl_hours,
    }
    script = (
        "TOKEN=$(python3 /tmp/_panda_admin_token.py); "
        "curl -fsS -X POST http://127.0.0.1:18000/api/admin/v1/chrome-tickets "
        "-H \"Authorization: Bearer $TOKEN\" -H 'Content-Type: application/json' "
        f"-d {json.dumps(json.dumps(payload))}"
    )
    proc = subprocess.run(["ssh", ssh_host, script], capture_output=True, timeout=60)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")
        raise RuntimeError(f"grok2api pool push via ssh failed: {err[:300]}")
    log("pool_push_grok2api_ssh", response=(proc.stdout or b"").decode("utf-8", "replace").strip())


def push_to_pool(pool_row: dict, *, ssh_host: str, ttl_hours: float, api_base: str, admin_token: str) -> None:
    if api_base and admin_token:
        push_to_grok2api(pool_row, api_base=api_base, admin_token=admin_token, ttl_hours=ttl_hours)
        return
    push_to_panda_go_api_via_ssh(pool_row, ssh_host=ssh_host, ttl_hours=ttl_hours)


def push_to_grok2api(pool_row: dict, *, api_base: str, admin_token: str, ttl_hours: float) -> None:
    payload = {
        "account_id": pool_row["account_id"],
        "statsig_meta": pool_row.get("statsig_meta") or "",
        "cookie": pool_row.get("cookie") or "",
        "user_agent": pool_row.get("user_agent") or "",
        "sign_source": pool_row.get("sign_source") or "",
        "ttl_hours": ttl_hours,
    }
    req = urllib.request.Request(
        f"{api_base}/api/admin/v1/chrome-tickets",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {admin_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"grok2api pool push failed: {exc.code} {err[:300]}") from exc
    log("pool_push_grok2api", response=body.strip())


def push_to_panda_ssh(pool_row: dict, ssh_host: str, ttl_hours: float) -> None:
    proc = subprocess.run(
        ["ssh", ssh_host, f"python3 {PANDA_POOL} push --ttl-hours {ttl_hours}"],
        input=json.dumps(pool_row, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")
        raise RuntimeError(f"pool push failed: {err[:300]}")
    log("pool_push", response=(proc.stdout or b"").decode("utf-8", "replace").strip())


def sync_scripts(ssh_host: str) -> None:
    for name in ("panda_ticket_pool.py", "panda_lite_with_ticket.py"):
        local = ROOT / "tools" / name
        subprocess.run(
            ["scp", str(local), f"{ssh_host}:/opt/grok2api/tools/{name}"],
            check=True,
            timeout=120,
        )


def mint_one(chrome, v1, acc: dict, *, timeout: int, ttl_hours: float, ssh_host: str, api_base: str, admin_token: str) -> bool:
    aid = int(acc["id"])
    signed = chrome.sign_with_chrome(acc["sso"], timeout_s=timeout, proxy=v1.PROXY)
    meta = signed.get("statsigMeta") or ""
    if not meta:
        log("mint_skip", account_id=aid, reason="no_statsig_meta", error=signed.get("error"))
        return False
    row = {
        "account_id": aid,
        "statsig_meta": meta,
        "cookie": sanitize_cookie(signed.get("cookie") or ""),
        "user_agent": chrome.UA,
        "sign_source": signed.get("source"),
    }
    push_to_pool(row, ssh_host=ssh_host, ttl_hours=ttl_hours, api_base=api_base, admin_token=admin_token)
    log("mint_ok", account_id=aid, meta_len=len(meta))
    return True


def main() -> None:
    if sys.platform == "win32" and "GROK_PW_HEADLESS" not in os.environ:
        os.environ["GROK_PW_HEADLESS"] = "0"
    for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "OPENSSL_CONF", "SSL_CERT_DIR"):
        os.environ.pop(k, None)

    parser = argparse.ArgumentParser(description="Mint Chrome tickets into Panda pool")
    parser.add_argument("--account-ids", required=True, help="comma-separated account ids")
    parser.add_argument("--sso-file", type=Path, default=DEFAULT_SSO)
    parser.add_argument("--ssh-host", default=SSH_HOST)
    parser.add_argument("--ttl-hours", type=float, default=12.0)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--api-base", default=GROK2API_BASE, help="grok2api base URL (preferred over SSH)")
    parser.add_argument("--admin-token", default=GROK2API_ADMIN_TOKEN, help="admin bearer token for grok2api")
    parser.add_argument("--per-account-delay", type=float, default=3.0)
    args = parser.parse_args()

    chrome, v1 = load_modules()
    accounts = load_accounts(args.sso_file)
    ids = [int(x.strip()) for x in args.account_ids.split(",") if x.strip()]

    if not (args.api_base and args.admin_token):
        sync_scripts(args.ssh_host)
    ok = 0
    for aid in ids:
        acc = accounts.get(aid)
        if not acc:
            log("mint_skip", account_id=aid, reason="sso_missing_in_file")
            continue
        if mint_one(
            chrome, v1, acc,
            timeout=args.timeout, ttl_hours=args.ttl_hours, ssh_host=args.ssh_host,
            api_base=args.api_base, admin_token=args.admin_token,
        ):
            ok += 1
        time.sleep(args.per_account_delay)
    log("done", requested=len(ids), minted=ok)


if __name__ == "__main__":
    main()
