#!/usr/bin/env python3
"""Extract grok x-statsig-id challenge constants + prove pure-HTTP chat/Lite.

Strategy:
  1) One Chrome session: intercept SPA signer digest input + capture a real ticket
  2) Rebuild statsig in pure Python (Twitter-family XOR/SHA256 layout)
  3) curl_cffi only for chat + Lite (+ N replays), no further browser

If pure rebuild mismatches, still record decode of live tickets for iteration.
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
TMP = ROOT / ".tmp" / "pure-http-grok"
PROXY = "http://127.0.0.1:7897"
CHAT_PATH = "/rest/app-chat/conversations/new"
EPOCH = 1682924400
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

sys.path.insert(0, str(ROOT / "tools"))


def log(**kw) -> None:
    print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **kw}, ensure_ascii=False), flush=True)


def b64pad(s: str) -> bytes:
    s = s.strip()
    pad = "=" * ((4 - len(s) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(s + pad)
    except Exception:
        return base64.b64decode(s + pad)


def decode_statsig(sig: str) -> dict:
    raw = bytearray(b64pad(sig))
    if not raw:
        return {"error": "empty"}
    key = raw[0]
    plain = bytearray([key]) + bytearray(b ^ key for b in raw[1:])
    # layouts observed: [key][meta~48][counter_le32][sha16][trailer]
    out = {
        "raw_len": len(raw),
        "xor_key": key,
        "plain_len": len(plain),
        "trailer": plain[-1] if plain else None,
    }
    if len(plain) >= 70:
        out["meta48_b64"] = base64.b64encode(bytes(plain[1:49])).decode()
        out["counter"] = struct.unpack("<I", bytes(plain[49:53]))[0]
        out["sha16_hex"] = bytes(plain[53:69]).hex()
        out["layout"] = "1+48+4+16+1"
    elif len(plain) >= 69:
        # 49-byte header variant from grok-web-api docs
        out["header49_hex"] = bytes(plain[1:50]).hex()
        out["counter"] = struct.unpack("<I", bytes(plain[50:54]))[0]
        out["sha16_hex"] = bytes(plain[54:70]).hex() if len(plain) >= 70 else None
        out["layout"] = "1+49+4+16+1?"
    return out


def generate_statsig_simple(method: str, path: str, meta48: bytes, fingerprint: str, *, n: int | None = None, key: int | None = None) -> str:
    """MoonS11 / simplified XCTID-style (no SVG animation)."""
    if len(meta48) != 48:
        raise ValueError(f"meta48 len {len(meta48)}")
    n = int(time.time() - EPOCH) if n is None else n
    ts = struct.pack("<I", n)
    msg = f"{method}!{path}!{n}obfiowerehiring{fingerprint}"
    digest = hashlib.sha256(msg.encode()).digest()[:16]
    key = random.randint(0, 255) if key is None else key
    block = meta48 + ts + digest + bytes([3])
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sso-json", type=Path, default=ROOT / ".tmp" / "web-sso-canary.json")
    ap.add_argument("--account-id", type=int, default=209)
    ap.add_argument("--replay-n", type=int, default=3)
    ap.add_argument("--proxy", default=PROXY)
    args = ap.parse_args()

    TMP.mkdir(parents=True, exist_ok=True)
    data = json.loads(args.sso_json.read_text(encoding="utf-8"))
    acc = next(a for a in data["accounts"] if int(a["id"]) == args.account_id)
    sso = acc["sso"]
    if sso.lower().startswith("sso="):
        sso = sso[4:]
    log(phase="account", id=acc["id"], name=acc.get("name", "")[:40])

    # --- Phase A: Chrome extract once ---
    from playwright.sync_api import sync_playwright
    import importlib.util

    spec = importlib.util.spec_from_file_location("v1", ROOT / "tools" / "web_http_chat_image_canary.v1.py")
    v1 = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(v1)

    digest_inputs: list[str] = []
    net_sigs: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=True,
            proxy={"server": args.proxy},
            args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
        context.add_init_script(v1.CAPTURE_HOOK)
        # Hook crypto.subtle.digest to capture preimage
        context.add_init_script(
            """
            (() => {
              globalThis.__grokDigestInputs = [];
              const wrap = (subtle) => {
                if (!subtle || subtle.__grokHooked) return;
                const orig = subtle.digest.bind(subtle);
                subtle.digest = async function(algo, data) {
                  try {
                    const u8 = data instanceof ArrayBuffer ? new Uint8Array(data)
                      : (data instanceof Uint8Array ? data : new Uint8Array(data));
                    const text = new TextDecoder().decode(u8);
                    if (text.includes('!') || text.includes('obfiowerehiring')) {
                      globalThis.__grokDigestInputs.push(text.slice(0, 500));
                    }
                  } catch (_) {}
                  return orig(algo, data);
                };
                subtle.__grokHooked = true;
              };
              try { wrap(crypto.subtle); } catch(_) {}
              const desc = Object.getOwnPropertyDescriptor(window, 'crypto');
              // also wrap after load
              document.addEventListener('DOMContentLoaded', () => { try { wrap(crypto.subtle); } catch(_){} });
            })();
            """
        )
        context.add_cookies(
            [
                {"name": "sso", "value": sso, "domain": ".grok.com", "path": "/", "secure": True, "httpOnly": True},
                {"name": "sso-rw", "value": sso, "domain": ".grok.com", "path": "/", "secure": True, "httpOnly": True},
            ]
        )
        page = context.new_page()

        def on_req(req):
            if "grok.com/rest/" not in (req.url or ""):
                return
            sig = req.headers.get("x-statsig-id") or ""
            if not sig or sig.startswith("eDA6") or sig.startswith("x0:"):
                return
            from urllib.parse import urlparse

            net_sigs.append({"path": urlparse(req.url).path, "method": req.method, "sig": sig})

        page.on("request", on_req)
        page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=90000)
        try:
            v1._dismiss_age_gate(page)
        except Exception:
            pass
        page.wait_for_timeout(8000)

        # Force a signed REST call via UI if possible
        try:
            for sel in ("textarea", '[contenteditable="true"]', 'div[role="textbox"]'):
                loc = page.locator(sel)
                if loc.count() == 0:
                    continue
                loc.first.click(timeout=3000)
                loc.first.fill("Reply with exactly: PONG")
                page.keyboard.press("Enter")
                page.wait_for_timeout(10000)
                break
        except Exception as exc:
            log(phase="ui_err", error=type(exc).__name__)

        # Also probe modes
        try:
            page.evaluate("async () => { try { await fetch('/rest/modes', {credentials:'include'}); } catch(e) {} }")
            page.wait_for_timeout(2000)
        except Exception:
            pass

        digest_inputs = page.evaluate("() => (globalThis.__grokDigestInputs || []).slice(-20)") or []
        js_caps = page.evaluate("() => (globalThis.__grokCapturedSigs || []).slice(-40)") or []
        meta = page.evaluate(
            """() => {
              const out = {};
              for (const m of document.querySelectorAll('meta')) {
                const n = m.getAttribute('name') || m.getAttribute('property') || '';
                if (/verif|chall|grok-site/i.test(n)) out[n] = m.getAttribute('content');
              }
              // unicode dash variants
              const all = [...document.querySelectorAll('meta')].map(m => ({
                name: m.getAttribute('name'), content: (m.getAttribute('content')||'').slice(0,120)
              })).filter(x => x.name && /verif|grok/i.test(x.name));
              out._all = all;
              out.cookie = document.cookie.slice(0,200);
              return out;
            }"""
        )
        html_ver = page.content()
        m = re.search(r'name="grok-site[^\"]*verification"\s+content="([^"]+)"', html_ver)
        verification = (m.group(1) if m else None) or (meta or {}).get("grok-site―verification")
        # find svg / loading-x animation in DOM
        svg_info = page.evaluate(
            """() => {
              const svgs = [...document.querySelectorAll('svg')].slice(0,5).map(s => s.outerHTML.slice(0,300));
              const anim = [...document.querySelectorAll('[id*=loading], [class*=loading], [id*=animation]')].slice(0,5).map(e => e.outerHTML.slice(0,200));
              return {svgs, anim, hasRuntime: !!globalThis.__grokBridgeRuntime};
            }"""
        )
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in context.cookies() if c.get("name") and c.get("value"))
        has_cf = "cf_clearance=" in cookie
        browser.close()

    log(phase="extract", digest_n=len(digest_inputs), net_sigs=len(net_sigs), js_caps=len(js_caps), has_cf=has_cf, verification_len=len(verification or ""))
    if digest_inputs:
        log(phase="digest_sample", samples=digest_inputs[:5])

    # pick best live sig for decode
    live = None
    for prefer in (CHAT_PATH, "/rest/modes"):
        for item in list(js_caps) + net_sigs:
            if item.get("path") == prefer and item.get("sig"):
                live = item
                break
        if live:
            break
    if not live:
        for item in list(js_caps) + net_sigs:
            if item.get("sig"):
                live = item
                break

    decoded = decode_statsig(live["sig"]) if live else {}
    log(phase="decode_live", path=(live or {}).get("path"), decoded={k: v for k, v in decoded.items() if k != "meta48_b64"}, meta48_len=len(b64pad(decoded["meta48_b64"])) if decoded.get("meta48_b64") else 0)

    constants = {
        "verification": verification,
        "verification_b64_len": len(verification or ""),
        "digest_inputs": digest_inputs[:10],
        "decoded_live": decoded,
        "live_path": (live or {}).get("path"),
        "svg_info": svg_info,
        "meta": meta,
    }
    (TMP / "constants.json").write_text(json.dumps(constants, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- Phase B: pure generate from decoded meta + digest fingerprint ---
    fingerprint = ""
    suffix = ""
    if digest_inputs:
        # expected: METHOD!PATH!COUNTER + suffix  OR  METHOD!PATH!COUNTER + obfiowerehiring + fp
        sample = digest_inputs[-1]
        if "obfiowerehiring" in sample:
            fingerprint = sample.split("obfiowerehiring", 1)[1]
            suffix = "obfiowerehiring" + fingerprint
        else:
            # after second !
            parts = sample.split("!")
            if len(parts) >= 3:
                # parts[2] starts with digits (counter) then suffix
                m2 = re.match(r"^-?\d+(.*)$", parts[2])
                if m2:
                    suffix = m2.group(1)
                    fingerprint = suffix

    meta48 = None
    if decoded.get("meta48_b64"):
        meta48 = b64pad(decoded["meta48_b64"])
        if len(meta48) != 48 and verification:
            try:
                meta48 = b64pad(verification)
            except Exception:
                pass
    elif verification:
        try:
            meta48 = b64pad(verification)
        except Exception:
            meta48 = None

    pure_ok = False
    pure_sig = None
    if meta48 and len(meta48) == 48 and fingerprint is not None:
        # If fingerprint empty, try empty and common
        for fp in ([fingerprint] if fingerprint else []) + ["", "0"]:
            try:
                pure_sig = generate_statsig_simple("POST", CHAT_PATH, meta48, fp)
                d2 = decode_statsig(pure_sig)
                log(phase="pure_gen", fp_len=len(fp), sig_len=len(pure_sig), decoded_counter=d2.get("counter"), trailer=d2.get("trailer"))
                pure_ok = True
                fingerprint = fp
                break
            except Exception as exc:
                log(phase="pure_gen_err", error=type(exc).__name__ + ":" + str(exc)[:120])

    # --- Phase C: HTTP only ---
    from curl_cffi import requests as crequests

    proxies = {"http": args.proxy, "https": args.proxy}

    def http_call(name: str, sig: str, message: str, *, enable_image: bool = False) -> dict:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://grok.com",
            "Referer": "https://grok.com/",
            "User-Agent": UA,
            "Cookie": f"sso={sso}; sso-rw={sso}" + (("; " + cookie) if cookie and "sso=" not in cookie else ""),
            "x-statsig-id": sig,
        }
        # Prefer full jar from capture when available
        if cookie and "cf_clearance=" in cookie:
            headers["Cookie"] = cookie if "sso=" in cookie else (cookie + f"; sso={sso}; sso-rw={sso}")
        t0 = time.time()
        r = crequests.post(
            "https://grok.com" + CHAT_PATH,
            headers=headers,
            json=chat_payload(message, enable_image=enable_image),
            proxies=proxies,
            impersonate="chrome146",
            timeout=120,
        )
        text = r.text or ""
        kind = "anti_bot" if (r.status_code == 403 and "anti-bot" in text.lower()) else (
            "image_ok" if (enable_image and ("assets.grok" in text or "generated" in text)) else (
                "chat_ok" if (r.status_code == 200 and ("PONG" in text or "token" in text.lower() or "message" in text.lower())) else f"http_{r.status_code}"
            )
        )
        row = {"name": name, "http": r.status_code, "kind": kind, "elapsed_s": round(time.time() - t0, 2), "bytes": len(text), "body_head": text[:220]}
        log(phase="http", **{k: v for k, v in row.items() if k != "body_head"}, body_head=row["body_head"][:120])
        return row

    rows = []
    # 1) live browser ticket replay (baseline pure HTTP, still needs prior capture once)
    if live and live.get("sig"):
        rows.append(http_call("live_ticket_text", live["sig"], "Reply with exactly: PONG"))
        rows.append(http_call("live_ticket_lite", live["sig"], "Drawing: a simple orange cat, realistic, clear details", enable_image=True))
        for i in range(args.replay_n):
            rows.append(http_call(f"live_replay_{i+1}", live["sig"], f"Reply with exactly: R{i+1}"))

    # 2) pure generated tickets (new counter each time — no Chrome)
    pure_rows = []
    if pure_ok and meta48 is not None:
        for i in range(max(2, args.replay_n)):
            sig = generate_statsig_simple("POST", CHAT_PATH, meta48, fingerprint or "")
            pure_rows.append(http_call(f"pure_text_{i+1}", sig, f"Reply with exactly: PURE{i+1}"))
        # one lite with fresh pure sig
        sig = generate_statsig_simple("POST", CHAT_PATH, meta48, fingerprint or "")
        pure_rows.append(http_call("pure_lite", sig, "Drawing: a simple blue square icon on white, minimal", enable_image=True))

    result = {
        "account_id": acc["id"],
        "has_cf": has_cf,
        "verification_present": bool(verification),
        "digest_inputs": digest_inputs[:5],
        "fingerprint_len": len(fingerprint or ""),
        "suffix_len": len(suffix or ""),
        "live_path": (live or {}).get("path"),
        "decoded": decoded,
        "live_http": rows,
        "pure_http": pure_rows,
        "verdict": {
            "live_http_text_ok": any(r["name"].startswith("live_ticket_text") and r["kind"] == "chat_ok" for r in rows)
            or any(r["name"].startswith("live_ticket_text") and r["http"] == 200 for r in rows),
            "live_http_image_ok": any(r["name"] == "live_ticket_lite" and r["kind"] in {"image_ok", "chat_ok"} for r in rows)
            or any(r["name"] == "live_ticket_lite" and r["http"] == 200 and "generated" in (r.get("body_head") or "") for r in rows),
            "pure_http_any_200": any(r.get("http") == 200 for r in pure_rows),
            "pure_http_any_chat_ok": any(r.get("kind") == "chat_ok" for r in pure_rows),
            "pure_http_any_image_ok": any(r.get("kind") == "image_ok" for r in pure_rows),
            "chrome_only_for_extract": True,
            "runtime_no_chrome_if_pure_works": any(r.get("kind") in {"chat_ok", "image_ok"} for r in pure_rows),
        },
    }
    (TMP / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log(phase="done", verdict=result["verdict"], out=str(TMP / "result.json"))
    return 0 if result["verdict"].get("pure_http_any_chat_ok") or result["verdict"].get("live_http_text_ok") else 4


if __name__ == "__main__":
    # clear bad SSL env
    os.environ.pop("SSL_CERT_FILE", None)
    os.environ.pop("REQUESTS_CA_BUNDLE", None)
    raise SystemExit(main())
