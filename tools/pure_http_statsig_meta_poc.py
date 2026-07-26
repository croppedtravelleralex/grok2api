#!/usr/bin/env python3
"""PoC: curl_cffi-only path to obtain Grok statsig_meta (no Chrome).

Unlike chatgpt2api Turnstile/chat-requirements, Grok uses HTML meta
(grok-site-verification) + x-statsig-id per request. This script tests
whether pure HTTP can pass CF and extract usable meta for chat/Lite.

Usage:
  python tools/pure_http_statsig_meta_poc.py --sso-file .tmp/web-sso-pin4.json --account-id 1574
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / ".tmp" / "pure-http-statsig-poc"
PROXY = os.environ.get("GROK_HTTP_PROXY", "http://127.0.0.1:7897")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
CHAT = "/rest/app-chat/conversations/new"
EPOCH = 1682924400
IMPERSONATE = os.environ.get("GROK_CURL_IMPERSONATE", "chrome146")

sys.path.insert(0, str(ROOT / "tools"))


def log(phase: str, **fields) -> None:
    row = {"ts": datetime.now(timezone.utc).isoformat(), "phase": phase, **fields}
    print(json.dumps(row, ensure_ascii=False), flush=True)


def b64d(value: str) -> bytes:
    pad = "=" * ((4 - len(value) % 4) % 4)
    return base64.b64decode(value + pad)


def extract_meta(html: str) -> str | None:
    for pat in (
        r'name=["\']grok-site[^"\']*verification["\'][^>]*content=["\']([^"\']+)',
        r'content=["\']([^"\']+)["\'][^>]*name=["\']grok-site[^"\']*verification',
        r'twitter:site-verification["\']?\s+content=["\']([^"\']+)',
    ):
        match = re.search(pat, html, re.I)
        if match:
            return match.group(1).strip()
    return None


def gen_statsig(meta48: bytes, fp: str, path: str = CHAT) -> str:
    n = int(time.time() - EPOCH)
    msg = f"POST!{path}!{n}obfiowerehiring{fp}"
    digest = hashlib.sha256(msg.encode()).digest()[:16]
    key = random.randint(0, 255)
    block = meta48 + struct.pack("<I", n) + digest + b"\x03"
    enc = bytearray([key]) + bytearray(b ^ key for b in block)
    return base64.b64encode(bytes(enc)).decode().rstrip("=")


def load_sso(sso_file: Path, account_id: int) -> dict:
    data = json.loads(sso_file.read_text(encoding="utf-8"))
    rows = data.get("accounts") if isinstance(data, dict) else data
    for row in rows:
        if int(row.get("id") or 0) == account_id and row.get("sso"):
            return row
    raise SystemExit(f"account {account_id} not in {sso_file}")


def chat_probe(session, cookie: str, meta_raw: bytes, fp: str, label: str) -> dict:
    from curl_cffi import requests as creq

    sig = gen_statsig(meta_raw, fp)
    started = time.perf_counter()
    resp = session.post(
        "https://grok.com" + CHAT,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://grok.com",
            "Referer": "https://grok.com/",
            "User-Agent": UA,
            "Cookie": cookie,
            "x-statsig-id": sig,
        },
        json={
            "temporary": True,
            "message": "Reply with exactly: POCOK",
            "modeId": "fast",
            "disableMemory": True,
            "enableImageGeneration": False,
            "fileAttachments": [],
            "imageAttachments": [],
        },
        timeout=60,
    )
    wall = round(time.perf_counter() - started, 3)
    body = (resp.text or "")[:200]
    kind = "chat_ok" if resp.status_code == 200 and "conversation" in body.lower() else (
        "anti_bot" if "anti-bot" in body.lower() else f"http_{resp.status_code}"
    )
    return {"case": label, "http": resp.status_code, "kind": kind, "wall_s": wall, "body": body}


def run_poc(sso_file: Path, account_id: int, *, lite: bool, proxy_url: str, impersonate: str) -> dict:
    from curl_cffi import requests as creq

    os.environ.pop("SSL_CERT_FILE", None)
    os.environ.pop("REQUESTS_CA_BUNDLE", None)
    TMP.mkdir(parents=True, exist_ok=True)

    acc = load_sso(sso_file, account_id)
    sso_cookie = f"sso={acc['sso']}; sso-rw={acc.get('sso_rw') or acc['sso']}"

    session = creq.Session(impersonate=impersonate, proxies={"http": proxy_url, "https": proxy_url})
    home_started = time.perf_counter()
    home = session.get("https://grok.com/", headers={"User-Agent": UA, "Accept": "text/html"}, timeout=45)
    home_wall = round(time.perf_counter() - home_started, 3)
    html = home.text or ""
    meta_b64 = extract_meta(html)
    meta_raw = b64d(meta_b64) if meta_b64 else b""
    jar = session.cookies.get_dict()

    home_row = {
        "http": home.status_code,
        "wall_s": home_wall,
        "cf_mitigated": home.headers.get("cf-mitigated"),
        "title_ok": "<title>Grok</title>" in html or "<title>grok</title>" in html.lower(),
        "meta_found": bool(meta_b64),
        "meta_bytes": len(meta_raw),
        "meta_is_48": len(meta_raw) == 48,
        "jar_keys": sorted(jar.keys()),
        "jar_has_cf": "cf_clearance" in jar,
        "response_bytes": len(home.content),
    }
    log("home", account_id=account_id, **home_row)
    (TMP / f"home-{account_id}.html").write_text(html[:30000], encoding="utf-8", errors="replace")

    probes: list[dict] = []
    if len(meta_raw) == 48:
        jar_ck = "; ".join(f"{k}={v}" for k, v in jar.items())
        for label, cookie, fp in (
            ("sso_only", sso_cookie, "142621100100"),
            ("jar+sso", (jar_ck + "; " if jar_ck else "") + sso_cookie, "142621100100"),
            ("sso_empty_fp", sso_cookie, ""),
        ):
            probes.append(chat_probe(session, cookie, meta_raw, fp, label))

    lite_row = None
    if lite and len(meta_raw) == 48:
        sig = gen_statsig(meta_raw, "142621100100")
        lite_started = time.perf_counter()
        lite = session.post(
            "https://grok.com" + CHAT,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://grok.com",
                "Referer": "https://grok.com/",
                "User-Agent": UA,
                "Cookie": sso_cookie,
                "x-statsig-id": sig,
            },
            json={
                "temporary": True,
                "message": "a red apple product photo",
                "modeId": "fast",
                "enableImageGeneration": True,
                "enableImageStreaming": True,
                "imageGenerationCount": 1,
                "disableMemory": True,
                "fileAttachments": [],
                "imageAttachments": [],
            },
            timeout=120,
        )
        lite_row = {
            "http": lite.status_code,
            "wall_s": round(time.perf_counter() - lite_started, 3),
            "has_image_marker": "image" in (lite.text or "").lower(),
            "body": (lite.text or "")[:240],
        }
        log("lite_probe", account_id=account_id, **lite_row)

    verdict = "fail"
    if home_row["http"] == 200 and home_row["meta_is_48"]:
        if any(p.get("kind") == "chat_ok" for p in probes):
            verdict = "partial_chat"
        if lite_row and lite_row.get("http") == 200 and lite_row.get("has_image_marker"):
            verdict = "lite_maybe"

    report = {
        "account_id": account_id,
        "impersonate": impersonate,
        "proxy": proxy_url,
        "home": home_row,
        "chat_probes": probes,
        "lite": lite_row,
        "verdict": verdict,
        "production_ready": verdict in {"lite_maybe"},
        "notes": [
            "Grok meta from HTML, not ChatGPT chat-requirements/Turnstile VM",
            "Production needs stable meta48 + signer fingerprint alignment",
            "CF clearance on curl_cffi session may still be required for asset download",
        ],
    }
    out = TMP / f"poc-{account_id}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log("summary", path=str(out), **{k: report[k] for k in ("verdict", "production_ready")})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Pure HTTP statsig_meta PoC for Grok")
    parser.add_argument("--sso-file", type=Path, default=ROOT / ".tmp" / "web-sso-pin4.json")
    parser.add_argument("--account-id", type=int, default=1574)
    parser.add_argument("--lite", action="store_true", help="also probe image generation SSE")
    parser.add_argument("--repeat", type=int, default=3, help="repeat home+chat matrix N times")
    parser.add_argument("--proxy-url", default=os.environ.get("GROK_HTTP_PROXY", PROXY))
    parser.add_argument("--impersonate", default=IMPERSONATE)
    args = parser.parse_args()
    proxy_url = args.proxy_url
    impersonate = args.impersonate

    runs = []
    for i in range(args.repeat):
        log("run_start", index=i + 1, total=args.repeat)
        runs.append(run_poc(args.sso_file, args.account_id, lite=args.lite, proxy_url=proxy_url, impersonate=impersonate))
        if i + 1 < args.repeat:
            time.sleep(3)

    success_home = sum(1 for r in runs if r["home"]["meta_is_48"])
    success_chat = sum(1 for r in runs if any(p.get("kind") == "chat_ok" for p in r["chat_probes"]))
    aggregate = {
        "repeat": args.repeat,
        "meta_extract_rate": round(success_home / max(len(runs), 1), 3),
        "chat_ok_rate": round(success_chat / max(len(runs), 1), 3),
        "runs": runs,
    }
    agg_path = TMP / "aggregate.json"
    agg_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(aggregate, ensure_ascii=False, indent=2))
    return 0 if success_home > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
