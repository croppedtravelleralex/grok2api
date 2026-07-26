#!/usr/bin/env python3
"""Zero-browser bootstrap: HTML verification meta via curl_cffi + chat."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import struct
import time
from pathlib import Path

from curl_cffi import requests as crequests

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / ".tmp" / "pure-http-grok"
PROXY = "http://127.0.0.1:7897"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
EPOCH = 1682924400
CHAT = "/rest/app-chat/conversations/new"


def b64d(s: str) -> bytes:
    return base64.b64decode(s + "=" * ((4 - len(s) % 4) % 4))


def gen(meta: bytes, fp: str) -> str:
    n = int(time.time() - EPOCH)
    dig = f"POST!{CHAT}!{n}obfiowerehiring{fp}"
    sha = hashlib.sha256(dig.encode()).digest()[:16]
    key = random.randint(0, 255)
    block = meta + struct.pack("<I", n) + sha + b"\x03"
    enc = bytearray([key]) + bytearray(b ^ key for b in block)
    return base64.b64encode(bytes(enc)).decode().rstrip("=")


def extract_meta(html: str) -> str | None:
    for pat in (
        r'name=["\']grok-site[^"\']*verification["\'][^>]*content=["\']([^"\']+)',
        r'content=["\']([^"\']+)["\'][^>]*name=["\']grok-site[^"\']*verification',
    ):
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1)
    return None


def payload(msg: str) -> dict:
    return {
        "temporary": True,
        "message": msg,
        "fileAttachments": [],
        "imageAttachments": [],
        "enableImageGeneration": False,
        "returnImageBytes": False,
        "modeId": "fast",
        "disableMemory": True,
        "sendFinalMetadata": True,
        "isAsyncChat": False,
        "collectionIds": [],
        "responseMetadata": {},
        "disableSearch": False,
        "disableTextFollowUps": True,
        "enableImageStreaming": False,
        "imageGenerationCount": 0,
        "forceConcise": False,
        "enableSideBySide": False,
        "forceSideBySide": False,
        "returnRawGrokInXaiRequest": False,
        "disabledConnectorIds": [],
        "deviceEnvInfo": {
            "darkModeEnabled": False,
            "devicePixelRatio": 2,
            "screenHeight": 1000,
            "screenWidth": 1000,
            "viewportHeight": 800,
            "viewportWidth": 1000,
        },
    }


def chat(cookie: str, meta: bytes, fp: str, name: str) -> dict:
    sig = gen(meta, fp)
    r = crequests.post(
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
        json=payload("Reply with exactly: ZBOK"),
        proxies={"http": PROXY, "https": PROXY},
        impersonate="chrome146",
        timeout=60,
    )
    text = r.text or ""
    kind = (
        "chat_ok"
        if r.status_code == 200 and "conversation" in text
        else ("anti_bot" if "anti-bot" in text.lower() else f"http_{r.status_code}")
    )
    row = {"case": name, "http": r.status_code, "kind": kind, "body": text[:120]}
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return row


def main() -> int:
    os.environ.pop("SSL_CERT_FILE", None)
    os.environ.pop("REQUESTS_CA_BUNDLE", None)
    keys = json.loads((TMP / "session_keys.json").read_text(encoding="utf-8"))
    meta_ext = b64d(keys["meta_b64"])
    fp = keys["fingerprint"]

    sess = crequests.Session(impersonate="chrome146", proxies={"http": PROXY, "https": PROXY})
    ip = sess.get("https://api.ipify.org?format=json", timeout=15).json()
    home = sess.get("https://grok.com/", headers={"User-Agent": UA, "Accept": "text/html"}, timeout=40)
    text = home.text or ""
    ver = extract_meta(text)
    raw = b64d(ver) if ver else b""
    jar = sess.cookies.get_dict()
    info = {
        "egress": ip,
        "home_http": home.status_code,
        "cf": home.headers.get("cf-mitigated"),
        "title_ok": "<title>Grok</title>" in text,
        "ver_len": len(ver or ""),
        "ver_bytes": len(raw),
        "meta_equals_extracted": (raw == meta_ext) if raw else False,
        "jar": sorted(jar.keys()),
        "jar_has_cf": "cf_clearance" in jar,
    }
    print(json.dumps({"phase": "home", **info}, ensure_ascii=False), flush=True)
    (TMP / "zero_browser_home_snip.html").write_text(text[:20000], encoding="utf-8", errors="replace")

    rows = []
    sso = f"sso={keys['sso']}; sso-rw={keys['sso']}"
    jar_ck = "; ".join(f"{k}={v}" for k, v in jar.items())
    if len(raw) == 48:
        rows.append(chat(sso, raw, fp, "ZB_html_meta_ext_fp_sso"))
        rows.append(chat((jar_ck + "; " if jar_ck else "") + sso, raw, fp, "ZB_html_meta_ext_fp_jar_sso"))
        rows.append(chat(sso, raw, "", "ZB_html_meta_empty_fp"))
        # try short fp variants if empty fails
        for trial_fp in ("0", "142621100100", fp):
            rows.append(chat(sso, raw, trial_fp, f"ZB_html_meta_fp_{trial_fp[:12] or 'empty'}"))
    rows.append(chat(sso, meta_ext, fp, "ZB_extracted_meta_ext_fp_sso_control"))

    # Lite image with SSO-only + extracted keys (no CF) — transferability of image path
    sig = gen(meta_ext, fp)
    r = crequests.post(
        "https://grok.com" + CHAT,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://grok.com",
            "Referer": "https://grok.com/",
            "User-Agent": UA,
            "Cookie": sso,
            "x-statsig-id": sig,
        },
        json={
            **payload("Drawing: a simple blue square, realistic, clear details"),
            "enableImageGeneration": True,
            "enableImageStreaming": True,
            "imageGenerationCount": 2,
            "message": "Drawing: a simple blue square, realistic, clear details",
        },
        proxies={"http": PROXY, "https": PROXY},
        impersonate="chrome146",
        timeout=120,
    )
    text = r.text or ""
    img = {
        "case": "Lite_sso_only_no_cf",
        "http": r.status_code,
        "kind": (
            "image_ok"
            if r.status_code == 200 and ("assets.grok" in text or "generated" in text)
            else ("anti_bot" if "anti-bot" in text.lower() else f"http_{r.status_code}")
        ),
        "body": text[:140],
    }
    print(json.dumps(img, ensure_ascii=False), flush=True)
    rows.append(img)

    out = {"home": info, "rows": rows}
    (TMP / "zero_browser_result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"phase": "done", "out": str(TMP / "zero_browser_result.json")}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
