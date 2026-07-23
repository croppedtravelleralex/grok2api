#!/usr/bin/env python3
"""Local Chrome sign → Panda HTTP Lite image (split ticket chain).

Architecture:
  1) 本机 Playwright channel=chrome 短签 + statsig_meta（可延长票寿命）
  2) 开票后立即 SSH 送票到 Panda（票即用）
  3) Panda 刷新 statsig + Lite HTTP + 下载 asset
  4) 本机 SCP 拉回图片

Doc: docs/http-reverse-lite-chain.md
Remote worker: tools/panda_lite_with_ticket.py
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / ".tmp"
DEFAULT_SSO = TMP / "web-sso-canary.json"
PANDA_TOOLS = "/opt/grok2api/tools"
SSH_HOST = os.environ.get("PANDA_SSH", "panda")
SIGN_ATTEMPTS = 3


def log(event: str, **kw) -> None:
    row = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **kw}
    print(json.dumps(row, ensure_ascii=False), flush=True)


def load_chrome_mod():
    spec = importlib.util.spec_from_file_location("chrome_http", ROOT / "tools" / "_chrome_channel_chat_image.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def load_v1():
    spec = importlib.util.spec_from_file_location("v1", ROOT / "tools" / "web_http_chat_image_canary.v1.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def load_accounts(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return list(data.get("accounts") or [])
    return list(data)


def fetch_panda_web_proxy(ssh_host: str) -> str:
    script = (
        "import base64,sqlite3\n"
        "from cryptography.hazmat.primitives.ciphers.aead import AESGCM\n"
        "key=None\n"
        "for line in open('/opt/grok2api/config.yaml'):\n"
        "  if 'credentialEncryptionKey' in line:\n"
        "    key=base64.b64decode(line.split(':',1)[1].strip().strip(chr(34)).strip(chr(39)))\n"
        "aes=AESGCM(key)\n"
        "blob=sqlite3.connect('/opt/grok2api/data/backend.db').execute(\"SELECT encrypted_proxy_url FROM egress_nodes WHERE scope='grok_web' AND enabled=1 ORDER BY health DESC,id ASC LIMIT 1\").fetchone()[0].strip()\n"
        "pad='='*((4-len(blob)%4)%4)\n"
        "raw=base64.b64decode(blob+pad)\n"
        "print(aes.decrypt(raw[:12],raw[12:],None).decode())\n"
    )
    proc = subprocess.run(
        ["ssh", ssh_host, "python3", "-"],
        input=script.encode("utf-8"),
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")
        raise RuntimeError(f"fetch panda proxy failed: {err[:300]}")
    proxy = (proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()[-1].strip()
    if not proxy.startswith("http"):
        raise RuntimeError(f"invalid panda proxy: {proxy[:80]}")
    return proxy


def proxy_host(proxy: str) -> str:
    return proxy.split("@")[-1] if "@" in proxy else proxy


def sanitize_cookie_for_ticket(cookie: str) -> str:
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


def pick_sig(chrome, acc: dict, signed: dict, v1, sign_proxy: str) -> tuple[str | None, str | None, list]:
    v1.PROXY = sign_proxy
    v1.PROXIES = {"http": sign_proxy, "https": sign_proxy}
    sig, cookie, probes = chrome.pick_working_sig(acc["sso"], signed)
    if not sig:
        for c in sorted(
            (signed.get("candidates") or []),
            key=lambda x: chrome.rank_path(str(x.get("path") or "")),
        ):
            path = str(c.get("path") or "")
            if chrome.valid_sig(c.get("sig") or "") and (
                path == chrome.CHAT_PATH or path.endswith("/conversations/new")
            ):
                sig = c["sig"]
                log("sig_fallback", path=path, reason="conversations_new_candidate")
                break
    if not sig and chrome.valid_sig(signed.get("statsigId") or ""):
        src = str(signed.get("source") or "")
        if "conversations/new" in src or src.startswith("turbopack:"):
            sig = signed["statsigId"]
            log("sig_fallback", path=src, reason="primary_statsig")
    if not sig and signed.get("statsigMeta"):
        log("sig_deferred", reason="statsig_meta_only_panda_refresh")
        sig = "meta-only"
    return sig, cookie, probes


def sign_with_retries(chrome, sso: str, proxy: str, timeout_s: int) -> dict:
    last: dict = {}
    for attempt in range(1, SIGN_ATTEMPTS + 1):
        log("sign_attempt", attempt=attempt)
        last = chrome.sign_with_chrome(sso, timeout_s=timeout_s, proxy=proxy)
        has_meta = bool(last.get("statsigMeta"))
        has_sig = bool(last.get("statsigId")) or any(
            chrome.valid_sig(c.get("sig") or "") for c in (last.get("candidates") or [])
        )
        if has_sig or has_meta:
            last["sign_attempt"] = attempt
            return last
        log("sign_retry", attempt=attempt, error=last.get("error"), notes=(last.get("notes") or [])[-3:])
        time.sleep(2 * attempt)
    return last


def scp_to_panda(local: Path, remote: str, ssh_host: str) -> None:
    subprocess.run(["scp", str(local), f"{ssh_host}:{remote}"], check=True, timeout=120)


def remote_script_hash(ssh_host: str) -> str:
    proc = subprocess.run(
        ["ssh", ssh_host, f"sha256sum {PANDA_TOOLS}/panda_lite_with_ticket.py 2>/dev/null || echo missing"],
        capture_output=True,
        timeout=30,
    )
    line = (proc.stdout or b"").decode("utf-8", "replace").strip().split()
    return line[0] if line else "missing"


def local_script_hash() -> str:
    data = (ROOT / "tools" / "panda_lite_with_ticket.py").read_bytes()
    return hashlib.sha256(data).hexdigest()


def sync_worker_if_needed(ssh_host: str, force: bool = False) -> None:
    local_h = local_script_hash()
    if not force and remote_script_hash(ssh_host) == local_h:
        log("panda_sync_skip", reason="hash_match")
        return
    log("panda_sync_script")
    scp_to_panda(ROOT / "tools" / "panda_lite_with_ticket.py", f"{PANDA_TOOLS}/panda_lite_with_ticket.py", ssh_host)


def run_panda_lite(ticket: dict, ssh_host: str) -> tuple[int, str]:
    payload = json.dumps(ticket, ensure_ascii=False)
    remote_script = f"{PANDA_TOOLS}/panda_lite_with_ticket.py"
    proc = subprocess.run(
        ["ssh", ssh_host, f"python3 {remote_script}"],
        input=payload.encode("utf-8"),
        capture_output=True,
        timeout=300,
    )
    stdout = (proc.stdout or b"").decode("utf-8", "replace")
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    saved_file = ""
    for line in stdout.splitlines():
        if line.strip().startswith("{"):
            print(line, flush=True)
            try:
                row = json.loads(line)
                if row.get("event") == "done" and row.get("saved_file"):
                    saved_file = str(row["saved_file"])
            except json.JSONDecodeError:
                pass
    if stderr.strip():
        print(stderr[-800:], file=sys.stderr)
    return proc.returncode, saved_file


def fetch_image_from_panda(saved_file: str, account_id: int, ssh_host: str) -> Path | None:
    if not saved_file:
        return None
    local_dir = TMP / "lite-images" / f"panda-ticket-{account_id}-{time.strftime('%Y%m%d-%H%M%S')}"
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / Path(saved_file).name
    proc = subprocess.run(
        ["scp", f"{ssh_host}:{saved_file}", str(local_path)],
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0:
        log("fetch_image_failed", remote=saved_file, stderr=(proc.stderr or b"").decode("utf-8", "replace")[:200])
        return None
    log("fetch_image_ok", path=str(local_path), bytes=local_path.stat().st_size)
    return local_path


def main() -> None:
    for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "OPENSSL_CONF", "SSL_CERT_DIR"):
        os.environ.pop(k, None)
    if sys.platform == "win32" and "GROK_PW_HEADLESS" not in os.environ:
        os.environ["GROK_PW_HEADLESS"] = "0"

    parser = argparse.ArgumentParser(description="Local Chrome ticket → Panda Lite HTTP")
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--sso-file", type=Path, default=DEFAULT_SSO)
    parser.add_argument("--prompt", default="Drawing: a red apple on white table, product photo")
    parser.add_argument(
        "--sign-proxy",
        choices=("local", "panda"),
        default="local",
        help="local=Mihomo 7897（默认）；panda=与 Panda grok_web 同 udeal 出口（本机 Chrome 常 RESET）",
    )
    parser.add_argument("--ssh-host", default=SSH_HOST)
    parser.add_argument("--skip-local-probe", action="store_true", default=sys.platform == "win32")
    parser.add_argument("--skip-panda", action="store_true", help="only sign + write ticket json")
    parser.add_argument("--force-sync", action="store_true", help="always scp worker script to Panda")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    chrome = load_chrome_mod()
    v1 = load_v1()
    accounts = load_accounts(args.sso_file)
    acc = next((a for a in accounts if int(a["id"]) == args.account_id), None)
    if not acc or not acc.get("sso"):
        raise SystemExit(f"account {args.account_id} not in {args.sso_file}")

    if args.sign_proxy == "panda":
        sign_proxy = fetch_panda_web_proxy(args.ssh_host)
    else:
        sign_proxy = v1.PROXY

    log(
        "sign_start",
        account_id=args.account_id,
        sign_proxy_host=proxy_host(sign_proxy),
        sign_proxy_mode=args.sign_proxy,
        headless=os.environ.get("GROK_PW_HEADLESS", "?"),
    )
    t0 = time.time()
    signed = sign_with_retries(chrome, acc["sso"], sign_proxy, args.timeout)
    log(
        "signed",
        elapsed_s=round(time.time() - t0, 1),
        source=signed.get("source"),
        has_sig=bool(signed.get("statsigId")),
        statsig_len=len(signed.get("statsigId") or ""),
        has_meta=bool(signed.get("statsigMeta")),
        meta_len=len(signed.get("statsigMeta") or ""),
        has_cf="cf_clearance" in (signed.get("cookie") or ""),
        error=signed.get("error"),
        notes=(signed.get("notes") or [])[-6:],
        attempt=signed.get("sign_attempt"),
    )
    if not signed.get("statsigId") and not signed.get("candidates") and not signed.get("statsigMeta"):
        raise SystemExit(2)

    sig, cookie, probes = pick_sig(chrome, acc, signed, v1, sign_proxy)
    log("local_probes", rows=probes, picked=bool(sig), sig_path=signed.get("source"))
    if not sig:
        raise SystemExit(2)

    ticket = {
        "version": 2,
        "account_id": args.account_id,
        "statsig": "" if sig == "meta-only" else sig,
        "statsig_meta": signed.get("statsigMeta") or "",
        "cookie": sanitize_cookie_for_ticket(cookie or ""),
        "user_agent": chrome.UA,
        "prompt": args.prompt,
        "signed_at": datetime.now(timezone.utc).isoformat(),
        "sign_proxy_host": proxy_host(sign_proxy),
        "sign_source": signed.get("source"),
    }
    ticket_path = TMP / f"chrome-ticket-{args.account_id}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    TMP.mkdir(parents=True, exist_ok=True)
    ticket_path.write_text(json.dumps(ticket, ensure_ascii=False, indent=2), encoding="utf-8")
    log("ticket_saved", path=str(ticket_path))

    if args.skip_panda:
        return

    sync_worker_if_needed(args.ssh_host, force=args.force_sync)

    log("panda_lite_start", account_id=args.account_id, ticket_age_ms=0)
    t1 = time.time()
    rc, saved_file = run_panda_lite(ticket, args.ssh_host)
    log("panda_elapsed_s", value=round(time.time() - t1, 1))
    if rc != 0:
        raise SystemExit(rc)

    local_image = fetch_image_from_panda(saved_file, args.account_id, args.ssh_host)
    log("done", ok=True, local_image=str(local_image) if local_image else None)


if __name__ == "__main__":
    main()
