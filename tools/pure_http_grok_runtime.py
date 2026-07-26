#!/usr/bin/env python3
"""Pure-HTTP grok chat/Lite using locked statsig layout (no browser at request time).

One-shot extract (Chrome) writes .tmp/pure-http-grok/session_keys.json:
  meta_b64, fingerprint, cookie(optional cf), sso
Then this script (or --extract) generates fresh x-statsig-id per request in Python.
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
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / ".tmp" / "pure-http-grok"
PROXY = "http://127.0.0.1:7897"
CHAT_PATH = "/rest/app-chat/conversations/new"
EPOCH = 1682924400
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


def log(**kw) -> None:
    print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **kw}, ensure_ascii=False), flush=True)


def b64decode(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.b64decode(s + pad)


def generate_statsig(method: str, path: str, meta48: bytes, fingerprint: str, *, n: int | None = None, key: int | None = None, trailer: bytes = b"\x03") -> str:
    if len(meta48) != 48:
        raise ValueError(f"meta48 len={len(meta48)}")
    n = int(time.time() - EPOCH) if n is None else n
    dig = f"{method}!{path}!{n}obfiowerehiring{fingerprint}"
    sha = hashlib.sha256(dig.encode()).digest()[:16]
    key = random.randint(0, 255) if key is None else key
    block = meta48 + struct.pack("<I", n) + sha + trailer
    enc = bytearray([key]) + bytearray(b ^ key for b in block)
    return base64.b64encode(bytes(enc)).decode().rstrip("=")


def chat_payload(message: str, *, enable_image: bool = False) -> dict:
    return {
        "collectionIds": [],
        "disabledConnectorIds": [],
        "deviceEnvInfo": {
            "darkModeEnabled": False,
            "devicePixelRatio": 2,
            "screenHeight": 1328,
            "screenWidth": 2056,
            "viewportHeight": 1083,
            "viewportWidth": 2056,
        },
        "disableMemory": True,
        "disableSearch": False,
        "disableSelfHarmShortCircuit": False,
        "disableTextFollowUps": True,
        "enableImageGeneration": enable_image,
        "enableImageStreaming": enable_image,
        "enableSideBySide": False,
        "fileAttachments": [],
        "forceConcise": False,
        "forceSideBySide": False,
        "imageAttachments": [],
        "imageGenerationCount": 2 if enable_image else 0,
        "isAsyncChat": False,
        "message": message,
        "modeId": "fast",
        "responseMetadata": {},
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "sendFinalMetadata": True,
        "temporary": True,
    }


def extract_session(account_id: int, sso_json: Path, proxy: str) -> dict:
    from playwright.sync_api import sync_playwright
    import importlib.util

    spec = importlib.util.spec_from_file_location("v1", ROOT / "tools" / "web_http_chat_image_canary.v1.py")
    v1 = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(v1)

    acc = next(a for a in json.loads(sso_json.read_text(encoding="utf-8"))["accounts"] if a["id"] == account_id)
    sso = acc["sso"][4:] if acc["sso"].lower().startswith("sso=") else acc["sso"]

    digests: list[str] = []
    sigs: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=True,
            proxy={"server": proxy},
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(user_agent=UA)
        ctx.add_init_script(v1.CAPTURE_HOOK)
        ctx.add_init_script(
            """
            (() => {
              globalThis.__grokDigestInputs = [];
              const capture = (d) => {
                try {
                  const u8 = d instanceof ArrayBuffer ? new Uint8Array(d)
                    : (d instanceof Uint8Array ? d : new Uint8Array(d));
                  const t = new TextDecoder().decode(u8);
                  if (t.includes('obfiowerehiring')) globalThis.__grokDigestInputs.push(t);
                } catch (e) {}
              };
              const proto = globalThis.SubtleCrypto && SubtleCrypto.prototype;
              if (proto && typeof proto.digest === 'function') {
                const original = proto.digest;
                Object.defineProperty(proto, 'digest', {
                  configurable: true,
                  value: function(a, d) {
                    capture(d);
                    return Reflect.apply(original, this, [a, d]);
                  },
                });
              } else {
                const original = crypto.subtle.digest.bind(crypto.subtle);
                crypto.subtle.digest = (a, d) => {
                  capture(d);
                  return original(a, d);
                };
              }
            })();
            """
        )
        ctx.add_cookies(
            [
                {"name": "sso", "value": sso, "domain": ".grok.com", "path": "/", "secure": True, "httpOnly": True},
                {"name": "sso-rw", "value": sso, "domain": ".grok.com", "path": "/", "secure": True, "httpOnly": True},
            ]
        )
        page = ctx.new_page()

        def on_req(req):
            if "grok.com/rest/" not in (req.url or ""):
                return
            sig = req.headers.get("x-statsig-id") or ""
            if not sig or sig.startswith("eDA6") or sig.startswith("x0:"):
                return
            sigs.append({"path": urlparse(req.url).path, "method": req.method, "sig": sig})

        page.on("request", on_req)
        page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(8000)
        page.evaluate("async () => { try { await fetch('/rest/modes', {credentials:'include'}); } catch (e) {} }")
        page.wait_for_timeout(2500)
        digests = page.evaluate("() => (globalThis.__grokDigestInputs || []).slice(-50)") or []
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())
        browser.close()

    # pick longest fingerprint digest (full animation fp)
    best = max(digests, key=len) if digests else ""
    m = re.match(r"^([A-Z]+)!([^!]+)!(\d+)obfiowerehiring(.*)$", best)
    if not m:
        raise RuntimeError(f"no digest parse: {best[:80]}")
    fp = m.group(4)
    # pair any sig with same counter for meta extract
    n = int(m.group(3))
    sha = hashlib.sha256(best.encode()).digest()[:16]
    meta = None
    trailer = b"\x03"
    for s in reversed(sigs):
        raw = bytearray(b64decode(s["sig"]))
        key = raw[0]
        plain = bytes([key]) + bytes(b ^ key for b in raw[1:])
        nb = struct.pack("<I", n)
        idx = plain.find(nb)
        if idx == 49 and plain[53:69] == sha:
            meta = plain[1:49]
            trailer = plain[69:] or b"\x03"
            break
    # fallback: decode any sig with counter near now
    if meta is None:
        for s in reversed(sigs):
            raw = bytearray(b64decode(s["sig"]))
            key = raw[0]
            plain = bytes([key]) + bytes(b ^ key for b in raw[1:])
            if len(plain) >= 70:
                meta = plain[1:49]
                trailer = plain[69:70] or b"\x03"
                break
    if meta is None or len(meta) != 48:
        raise RuntimeError("meta48 not recovered")

    keys = {
        "account_id": account_id,
        "sso": sso,
        "meta_b64": base64.b64encode(meta).decode(),
        "fingerprint": fp,
        "trailer_hex": trailer.hex(),
        "cookie": cookie,
        "has_cf": "cf_clearance=" in cookie,
        "sample_digest": best,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    (TMP / "session_keys.json").write_text(json.dumps(keys, ensure_ascii=False, indent=2), encoding="utf-8")
    log(phase="extracted", fp_len=len(fp), meta_len=len(meta), has_cf=keys["has_cf"], digests=len(digests))
    return keys


def http_once(keys: dict, name: str, message: str, *, enable_image: bool, proxy: str) -> dict:
    from curl_cffi import requests as crequests

    meta = b64decode(keys["meta_b64"])
    fp = keys["fingerprint"]
    trailer = bytes.fromhex(keys.get("trailer_hex") or "03")
    sig = generate_statsig("POST", CHAT_PATH, meta, fp, trailer=trailer)
    cookie = keys.get("cookie") or ""
    sso = keys["sso"]
    if "sso=" not in cookie:
        cookie = (cookie + "; " if cookie else "") + f"sso={sso}; sso-rw={sso}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://grok.com",
        "Referer": "https://grok.com/",
        "User-Agent": UA,
        "Cookie": cookie,
        "x-statsig-id": sig,
    }
    t0 = time.time()
    try:
        r = crequests.post(
            "https://grok.com" + CHAT_PATH,
            headers=headers,
            json=chat_payload(message, enable_image=enable_image),
            proxies={"http": proxy, "https": proxy},
            impersonate="chrome146",
            timeout=120,
        )
        text = r.text or ""
        kind = "anti_bot" if r.status_code == 403 and "anti-bot" in text.lower() else (
            "image_ok" if enable_image and ("assets.grok" in text or "generated" in text) else (
                "chat_ok" if r.status_code == 200 and ("result" in text or "PONG" in text or "PURE" in text or "token" in text.lower()) else f"http_{r.status_code}"
            )
        )
        row = {"name": name, "http": r.status_code, "kind": kind, "elapsed_s": round(time.time() - t0, 2), "bytes": len(text), "body_head": text[:220], "sig_len": len(sig)}
    except Exception as exc:
        row = {"name": name, "http": 0, "kind": "error", "elapsed_s": round(time.time() - t0, 2), "error": type(exc).__name__ + ":" + str(exc)[:160]}
    log(phase="http", **{k: v for k, v in row.items() if k != "body_head"}, body_head=(row.get("body_head") or "")[:100])
    return row


def main() -> int:
    os.environ.pop("SSL_CERT_FILE", None)
    os.environ.pop("REQUESTS_CA_BUNDLE", None)
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract", action="store_true", help="Chrome once to refresh session_keys.json")
    ap.add_argument("--account-id", type=int, default=209)
    ap.add_argument("--sso-json", type=Path, default=ROOT / ".tmp" / "web-sso-canary.json")
    ap.add_argument("--proxy", default=PROXY)
    ap.add_argument("--replay-n", type=int, default=3)
    args = ap.parse_args()
    TMP.mkdir(parents=True, exist_ok=True)

    keys_path = TMP / "session_keys.json"
    if args.extract or not keys_path.exists():
        keys = extract_session(args.account_id, args.sso_json, args.proxy)
    else:
        keys = json.loads(keys_path.read_text(encoding="utf-8"))
        log(phase="load_keys", account_id=keys.get("account_id"), fp_len=len(keys.get("fingerprint") or ""), has_cf=keys.get("has_cf"))

    rows = []
    rows.append(http_once(keys, "pure_text", "Reply with exactly: PUREOK", enable_image=False, proxy=args.proxy))
    rows.append(http_once(keys, "pure_lite", "Drawing: a simple orange cat, realistic, clear details", enable_image=True, proxy=args.proxy))
    for i in range(args.replay_n):
        rows.append(http_once(keys, f"pure_replay_{i+1}", f"Reply with exactly: R{i+1}", enable_image=False, proxy=args.proxy))
        time.sleep(0.3)

    verdict = {
        "text_ok": any(r["name"] == "pure_text" and r.get("kind") == "chat_ok" for r in rows),
        "image_ok": any(r["name"] == "pure_lite" and r.get("kind") == "image_ok" for r in rows),
        "replay_ok_n": sum(1 for r in rows if r["name"].startswith("pure_replay_") and r.get("kind") == "chat_ok"),
        "no_chrome_at_request_time": True,
        "statsig_generated_in_python": True,
    }
    verdict["pass"] = verdict["text_ok"] and verdict["image_ok"]
    out = {"verdict": verdict, "rows": rows, "keys_meta": {k: keys.get(k) for k in ("account_id", "fp_len", "has_cf", "extracted_at") if k in keys or k == "fp_len"}}
    out["keys_meta"]["fp_len"] = len(keys.get("fingerprint") or "")
    out["keys_meta"]["has_cf"] = keys.get("has_cf")
    out["keys_meta"]["extracted_at"] = keys.get("extracted_at")
    (TMP / "pure_runtime_result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(phase="done", verdict=verdict, out=str(TMP / "pure_runtime_result.json"))
    return 0 if verdict["pass"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
