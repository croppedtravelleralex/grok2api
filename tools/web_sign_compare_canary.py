#!/usr/bin/env python3
"""Compare: in-browser fetch vs curl_cffi vs tls-client replay with same SSO+sig+cookies."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from curl_cffi import requests as crequests
from playwright.sync_api import sync_playwright

PROXY = "http://127.0.0.1:7897"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
CHAT_PATH = "/rest/app-chat/conversations/new"

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

INIT = r"""
(() => {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    try {
      const headers = new Headers(init.headers || {});
      const sig = headers.get('x-statsig-id');
      const url = String(typeof input === 'string' ? input : input.url || '');
      if (sig && url.includes('/rest/')) {
        document.documentElement.setAttribute('data-last-sig', sig);
        document.documentElement.setAttribute('data-last-sig-url', url);
      }
    } catch (_) {}
    return nativeFetch(input, init);
  };
})();
"""


def load_sso(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return next(a for a in data["accounts"] if a.get("sso"))


def browser_phase(sso: str) -> dict:
    token = sso.strip()
    if token.lower().startswith("sso="):
        token = token[4:].strip()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, proxy={"server": PROXY})
        context = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 900})
        context.add_init_script(INIT)
        context.add_cookies(
            [
                {"name": "sso", "value": token, "domain": ".grok.com", "path": "/"},
                {"name": "sso-rw", "value": token, "domain": ".grok.com", "path": "/"},
            ]
        )
        page = context.new_page()
        rest_sigs: list[tuple[str, str]] = []

        def on_req(req):
            sig = req.headers.get("x-statsig-id")
            if sig and "/rest/" in req.url:
                rest_sigs.append((req.url, sig))

        page.on("request", on_req)
        page.goto("https://grok.com/", wait_until="commit", timeout=60000)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(10000)

        # Prefer a signature that was used for conversations/new; else any /rest/ sig.
        signature = ""
        sig_url = ""
        for url, sig in reversed(rest_sigs):
            if CHAT_PATH in url:
                signature, sig_url = sig, url
                break
        if not signature and rest_sigs:
            sig_url, signature = rest_sigs[-1]

        # Also try in-page chat fetch (browser-native), writing result to DOM without evaluate CSP issues:
        # use page.request from Playwright API context (not page JS) - actually page.request is separate cookie jar.
        # Use locator click? Too fragile.
        # CDP fetch via Fetch.enable? Skip - use route.

        cookies = context.cookies("https://grok.com/")
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        # In-browser chat using Playwright's request with storage state... better:
        # Intercept and fulfill? No - send via page.evaluate is CSP blocked.
        # Use context.request with cookies from context:
        api = context.request
        headers = {
            "content-type": "application/json",
            "origin": "https://grok.com",
            "referer": "https://grok.com/",
            "x-statsig-id": signature,
        }
        t0 = time.time()
        # Fresh sign for conversations/new: trigger by asking page to navigate soft?
        # If we only have rate-limits sig, chat may fail. Capture more by waiting.
        if CHAT_PATH not in sig_url:
            page.wait_for_timeout(5000)
            for url, sig in reversed(rest_sigs):
                if CHAT_PATH in url:
                    signature, sig_url = sig, url
                    break
            headers["x-statsig-id"] = signature

        browser_http = None
        browser_body = ""
        if signature:
            resp = api.post(
                "https://grok.com" + CHAT_PATH,
                headers=headers,
                data=json.dumps(PAYLOAD),
                timeout=90000,
            )
            browser_http = resp.status
            browser_body = resp.text()[:400]
        browser.close()

    return {
        "signature": signature,
        "sig_url": sig_url,
        "sig_len": len(signature or ""),
        "rest_sig_count": len(rest_sigs),
        "cookie": cookie_header,
        "cookie_names": sorted({c["name"] for c in cookies}),
        "browser_api_http": browser_http,
        "browser_api_body": browser_body.replace("\n", " "),
        "browser_api_elapsed": round(time.time() - t0, 2) if signature else None,
    }


def http_replay(cookie: str, signature: str, engine: str) -> dict:
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
        "user-agent": UA,
        "cookie": cookie,
        "x-statsig-id": signature,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    t0 = time.time()
    if engine == "curl_cffi":
        resp = crequests.post(
            "https://grok.com" + CHAT_PATH,
            headers=headers,
            json=PAYLOAD,
            impersonate="chrome131",
            proxies={"http": PROXY, "https": PROXY},
            timeout=90,
        )
        status, body = resp.status_code, resp.text[:400]
    else:
        raise ValueError(engine)
    return {
        "engine": engine,
        "http": status,
        "elapsed_s": round(time.time() - t0, 2),
        "has_pong": "PONG" in body,
        "body_prefix": body.replace("\n", " "),
    }


def main() -> None:
    acc = load_sso(Path(sys.argv[1] if len(sys.argv) > 1 else ".tmp/web-sso-canary.json"))
    print("account_id", acc["id"])
    boot = browser_phase(acc["sso"])
    safe = {k: boot[k] for k in boot if k != "cookie"}
    print("bootstrap", json.dumps(safe, ensure_ascii=False))
    if not boot["signature"]:
        raise SystemExit("no signature captured from browser network")
    replay = http_replay(boot["cookie"], boot["signature"], "curl_cffi")
    print("replay", json.dumps(replay, ensure_ascii=False))
    out = {
        "account_id": acc["id"],
        "bootstrap": safe,
        "replay": replay,
        "verdict": {
            "browser_api_ok": (boot.get("browser_api_http") or 0) < 400,
            "pure_http_ok": replay["http"] < 400 and (replay["has_pong"] or "token" in replay["body_prefix"]),
        },
    }
    Path(".tmp/web-sign-compare-result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("verdict", json.dumps(out["verdict"]))


if __name__ == "__main__":
    main()
