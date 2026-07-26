#!/usr/bin/env python3
"""Canary: path-correct Statsig for conversations/new, then in-page vs curl_cffi."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from curl_cffi import requests as crequests
from playwright.sync_api import sync_playwright

PROXY = "http://127.0.0.1:7897"
PROXIES = {"http": PROXY, "https": PROXY}
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CHAT_PATH = "/rest/app-chat/conversations/new"
SIGNER_MODULE_ID = 4629918

PAYLOAD = {
    "collectionIds": [],
    "disabledConnectorIds": [],
    "deviceEnvInfo": {
        "darkModeEnabled": False,
        "devicePixelRatio": 2,
        "screenHeight": 1080,
        "screenWidth": 1920,
        "viewportHeight": 900,
        "viewportWidth": 1400,
    },
    "disableMemory": True,
    "disableSearch": True,
    "disableSelfHarmShortCircuit": False,
    "disableTextFollowUps": True,
    "enableImageGeneration": False,
    "enableImageStreaming": False,
    "enableSideBySide": True,
    "fileAttachments": [],
    "forceConcise": True,
    "forceSideBySide": False,
    "imageAttachments": [],
    "imageGenerationCount": 0,
    "isAsyncChat": False,
    "message": "Reply with exactly: PONG",
    "modeId": "fast",
    "responseMetadata": {},
    "returnImageBytes": False,
    "returnRawGrokInXaiRequest": False,
    "sendFinalMetadata": True,
    "temporary": True,
}

# Capture Turbopack runtime early; sign+fetch only after body hydrated.
# Results written to DOM attrs (CSP blocks page.evaluate).
INIT = f"""
(() => {{
  const nativePush = Array.prototype.push;
  const queue = [];
  const wrap = (entry) => {{
    if (!Array.isArray(entry) || typeof entry[entry.length - 1] !== 'function') return entry;
    const original = entry[entry.length - 1];
    entry[entry.length - 1] = function (...args) {{
      if (args[0]) globalThis.__grokBridgeRuntime = args[0];
      return original.apply(this, args);
    }};
    return entry;
  }};
  queue.push = function (...entries) {{
    return nativePush.apply(this, entries.map(wrap));
  }};
  globalThis.TURBOPACK = queue;

  const net = [];
  globalThis.__grokNetSigs = net;
  const origFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {{}}) => {{
    try {{
      const headers = new Headers(init.headers || {{}});
      const sig = headers.get('x-statsig-id') || '';
      const url = String(typeof input === 'string' ? input : (input && input.url) || '');
      if (sig && url.includes('/rest/')) {{
        let path = url;
        try {{ path = new URL(url, location.origin).pathname; }} catch (_) {{}}
        net.push({{ path, sig, t: Date.now() }});
        document.documentElement.setAttribute('data-net-sig-count', String(net.length));
        document.documentElement.setAttribute('data-net-last-path', path);
        document.documentElement.setAttribute('data-net-last-sig', sig);
      }}
    }} catch (_) {{}}
    return origFetch(input, init);
  }};

  const path = {CHAT_PATH!r};
  const moduleId = {SIGNER_MODULE_ID};
  const payload = {json.dumps(PAYLOAD)};
  let phase = 'wait-runtime';

  const set = (k, v) => document.documentElement.setAttribute(k, String(v == null ? '' : v).slice(0, 800));

  const run = async () => {{
    if (phase !== 'wait-runtime') return;
    if (!globalThis.__grokBridgeRuntime) return;
    if (!document.body || document.body.childNodes.length < 3) return;
    // Wait a bit more for SPA modules to settle.
    phase = 'signing';
    set('data-phase', phase);
    try {{
      const mod = await globalThis.__grokBridgeRuntime.A(moduleId);
      const signer = mod.default();
      const signature = await signer(path, 'POST');
      set('data-sig', signature || '');
      set('data-sig-len', (signature || '').length);
      phase = 'fetching';
      set('data-phase', phase);
      const headers = new Headers({{
        'content-type': 'application/json',
        'x-statsig-id': signature,
      }});
      const t0 = Date.now();
      const resp = await origFetch(path, {{
        method: 'POST',
        headers,
        credentials: 'include',
        cache: 'no-store',
        body: JSON.stringify(payload),
      }});
      const text = await resp.text();
      set('data-page-http', resp.status);
      set('data-page-elapsed', Date.now() - t0);
      set('data-page-body', text.slice(0, 500).replace(/\\s+/g, ' '));
      set('data-page-ok', String(resp.status < 400 && (text.includes('PONG') || text.includes('token') || text.includes('messageTag'))));
      phase = 'done';
      set('data-phase', phase);
    }} catch (err) {{
      phase = 'error';
      set('data-phase', phase);
      set('data-sig-err', String(err && (err.stack || err.message) || err).slice(0, 500));
      // Allow one more retry after delay by resetting phase later via interval.
      setTimeout(() => {{ phase = 'wait-runtime'; }}, 3000);
    }}
  }};
  setInterval(run, 1000);
}})();
"""


def load_sso(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return next(a for a in data["accounts"] if a.get("sso"))


def browser_sign_and_page_fetch(sso: str, timeout_s: float = 150) -> dict:
    token = sso.strip()
    if token.lower().startswith("sso="):
        token = token[4:].strip()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": PROXY},
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 900})
        context.add_init_script(INIT)
        context.add_cookies(
            [
                {"name": "sso", "value": token, "domain": ".grok.com", "path": "/"},
                {"name": "sso-rw", "value": token, "domain": ".grok.com", "path": "/"},
            ]
        )
        page = context.new_page()
        pw_caps: list[dict] = []

        def on_req(req):
            if "/rest/" not in req.url:
                return
            sig = req.headers.get("x-statsig-id") or ""
            if not sig:
                return
            from urllib.parse import urlparse

            pw_caps.append({"path": urlparse(req.url).path, "method": req.method, "sig": sig})

        page.on("request", on_req)
        page.goto("https://grok.com/", wait_until="commit", timeout=90000)
        page.wait_for_load_state("domcontentloaded", timeout=90000)
        page.wait_for_timeout(5000)

        deadline = time.time() + timeout_s
        snap = {}
        while time.time() < deadline:
            html = page.locator("html")
            snap = {
                "phase": html.get_attribute("data-phase") or "",
                "sig": html.get_attribute("data-sig") or "",
                "sig_len": html.get_attribute("data-sig-len") or "",
                "sig_err": html.get_attribute("data-sig-err") or "",
                "page_http": html.get_attribute("data-page-http") or "",
                "page_elapsed": html.get_attribute("data-page-elapsed") or "",
                "page_body": html.get_attribute("data-page-body") or "",
                "page_ok": html.get_attribute("data-page-ok") or "",
                "net_sig_count": html.get_attribute("data-net-sig-count") or "",
                "net_last_path": html.get_attribute("data-net-last-path") or "",
                "net_last_sig": html.get_attribute("data-net-last-sig") or "",
                "title": page.title(),
                "url": page.url,
            }
            if snap["phase"] in {"done", "error"} and (snap["sig"] or snap["sig_err"]):
                if snap["phase"] == "done" or time.time() > deadline - 5:
                    break
            page.wait_for_timeout(800)

        cookies = context.cookies("https://grok.com/")
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        try:
            page.screenshot(path=".tmp/web-path-sign.png")
        except Exception:  # noqa: BLE001
            pass
        browser.close()

    chat_net = [c for c in pw_caps if CHAT_PATH in (c.get("path") or "")]
    return {
        "snap": snap,
        "cookie": cookie_header,
        "cookie_names": sorted({c["name"] for c in cookies}),
        "pw_rest_count": len(pw_caps),
        "pw_paths": sorted({c["path"] for c in pw_caps})[:20],
        "pw_chat_count": len(chat_net),
        "pw_chat_sig_prefix": (chat_net[-1]["sig"][:28] if chat_net else ""),
    }


def curl_replay(cookie: str, signature: str) -> dict:
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
        "user-agent": UA,
        "cookie": cookie,
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-statsig-id": signature,
    }
    t0 = time.time()
    resp = crequests.post(
        "https://grok.com" + CHAT_PATH,
        headers=headers,
        json=PAYLOAD,
        impersonate="chrome131",
        proxies=PROXIES,
        timeout=90,
    )
    body = resp.text[:500]
    return {
        "http": resp.status_code,
        "elapsed_s": round(time.time() - t0, 2),
        "cf": resp.headers.get("cf-mitigated"),
        "has_pong": "PONG" in body,
        "has_token": '"token"' in body or "messageTag" in body,
        "body_prefix": body.replace("\n", " "),
    }


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else ".tmp/web-sso-canary.json")
    acc = load_sso(src)
    print(f"account_id={acc['id']} name={acc.get('name','')}")
    print("=== browser path-sign + in-page fetch ===")
    boot = browser_sign_and_page_fetch(acc["sso"])
    snap = boot["snap"]
    print(
        json.dumps(
            {
                "title": snap.get("title"),
                "url": snap.get("url"),
                "phase": snap.get("phase"),
                "sig_len": snap.get("sig_len") or len(snap.get("sig") or ""),
                "sig_prefix": (snap.get("sig") or "")[:28],
                "sig_err": (snap.get("sig_err") or "")[:200],
                "page_http": snap.get("page_http"),
                "page_ok": snap.get("page_ok"),
                "page_elapsed": snap.get("page_elapsed"),
                "page_body": (snap.get("page_body") or "")[:220],
                "net_sig_count": snap.get("net_sig_count"),
                "net_last_path": snap.get("net_last_path"),
                "cookie_names": boot["cookie_names"],
                "pw_rest_count": boot["pw_rest_count"],
                "pw_paths": boot["pw_paths"],
                "pw_chat_count": boot["pw_chat_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    signature = snap.get("sig") or ""
    # Prefer path-correct network sig if signer failed but chat was somehow issued.
    if not signature and boot.get("pw_chat_sig_prefix"):
        # Full sig not stored in summary; re-get from snap net
        signature = snap.get("net_last_sig") or ""
        if snap.get("net_last_path") != CHAT_PATH:
            signature = ""

    print("=== curl_cffi replay ===")
    if not signature:
        replay = {"http": None, "error": "no path-correct signature"}
        print(json.dumps(replay, ensure_ascii=False))
    else:
        replay = curl_replay(boot["cookie"], signature)
        print(json.dumps(replay, ensure_ascii=False))

    page_http = int(snap.get("page_http") or 0) if str(snap.get("page_http") or "").isdigit() else 0
    verdict = {
        "signer_ok": bool(snap.get("sig")),
        "in_page_chat_ok": page_http > 0 and page_http < 400 and (snap.get("page_ok") == "true"),
        "in_page_http": page_http or None,
        "pure_http_ok": bool(
            isinstance(replay.get("http"), int)
            and replay["http"] < 400
            and (replay.get("has_pong") or replay.get("has_token"))
        ),
        "pure_http": replay.get("http"),
    }
    print("verdict", json.dumps(verdict, ensure_ascii=False))
    out = {
        "account_id": acc["id"],
        "boot": {k: boot[k] for k in boot if k != "cookie"},
        "replay": replay,
        "verdict": verdict,
    }
    Path(".tmp/web-path-sign-result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("wrote .tmp/web-path-sign-result.json")


if __name__ == "__main__":
    main()
