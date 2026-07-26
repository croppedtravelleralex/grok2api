#!/usr/bin/env python3
"""Canary: Chromium only to obtain Statsig (+cookies); chat via pure HTTP curl_cffi."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from curl_cffi import requests as crequests
from playwright.sync_api import sync_playwright

PROXY = "http://127.0.0.1:7897"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
SIGNER_MODULE_ID = 4629918
CHAT_PATH = "/rest/app-chat/conversations/new"

INIT_SCRIPT = f"""
(() => {{
  const queue = [];
  const nativePush = Array.prototype.push;
  const wrap = entry => {{
    if (!Array.isArray(entry) || typeof entry[entry.length - 1] !== 'function') return entry;
    const original = entry[entry.length - 1];
    entry[entry.length - 1] = function(...args) {{
      if (args[0]) globalThis.__grokBridgeRuntime = args[0];
      return original.apply(this, args);
    }};
    return entry;
  }};
  queue.push = function(...entries) {{ return nativePush.apply(this, entries.map(wrap)); }};
  globalThis.TURBOPACK = queue;

  // Capture any page-issued Statsig headers.
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {{}}) => {{
    try {{
      const headers = new Headers(init.headers || {{}});
      const sig = headers.get('x-statsig-id');
      if (sig) document.documentElement.setAttribute('data-sig-captured', sig);
    }} catch (_) {{}}
    return nativeFetch(input, init);
  }};

  const path = {CHAT_PATH!r};
  const method = 'POST';
  const moduleId = {SIGNER_MODULE_ID};
  let done = false;
  const trySign = async () => {{
    if (done || !globalThis.__grokBridgeRuntime) return;
    if (!document.body || document.body.childNodes.length < 1) return;
    done = true;
    try {{
      const signerModule = await globalThis.__grokBridgeRuntime.A(moduleId);
      const signer = signerModule.default();
      const signature = await signer(path, method);
      document.documentElement.setAttribute('data-sig', String(signature || ''));
    }} catch (error) {{
      done = false; // allow retry
      document.documentElement.setAttribute(
        'data-sig-err',
        String(error && (error.stack || error.message) || error).slice(0, 400)
      );
    }}
  }};
  setInterval(trySign, 800);
}})();
"""


def load_accounts(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [a for a in data.get("accounts", []) if a.get("sso")]


def chat_payload() -> dict:
    return {
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


def bootstrap_sign(sso: str, timeout_s: float = 120) -> dict:
    captured_from_network: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            proxy={"server": PROXY},
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 900})
        context.add_init_script(INIT_SCRIPT)
        token = sso.strip()
        if token.lower().startswith("sso="):
            token = token[4:].strip()
        context.add_cookies(
            [
                {"name": "sso", "value": token, "domain": ".grok.com", "path": "/"},
                {"name": "sso-rw", "value": token, "domain": ".grok.com", "path": "/"},
            ]
        )
        page = context.new_page()

        def on_request(req) -> None:
            sig = req.headers.get("x-statsig-id")
            if sig and "grok.com" in req.url:
                captured_from_network.append(sig)

        page.on("request", on_request)
        page.goto("https://grok.com/", wait_until="commit", timeout=int(timeout_s * 1000))
        page.wait_for_load_state("domcontentloaded", timeout=int(timeout_s * 1000))
        # Give SPA time to hydrate / fire its own signed calls.
        page.wait_for_timeout(8000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass

        deadline = time.time() + timeout_s
        signature = ""
        sig_err = ""
        while time.time() < deadline:
            signature = (
                page.locator("html").get_attribute("data-sig")
                or page.locator("html").get_attribute("data-sig-captured")
                or ""
            )
            if not signature and captured_from_network:
                signature = captured_from_network[-1]
            sig_err = page.locator("html").get_attribute("data-sig-err") or ""
            if signature:
                break
            page.wait_for_timeout(500)

        cookies = context.cookies("https://grok.com/")
        title = page.title()
        browser.close()

    if not signature:
        raise RuntimeError(
            f"sign failed title={title!r} err={sig_err!r} network_caps={len(captured_from_network)}"
        )
    cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    return {
        "signature": signature,
        "cookie": cookie_header,
        "cookie_names": sorted({c["name"] for c in cookies}),
        "title": title,
        "network_caps": len(captured_from_network),
    }


def pure_http_chat(cookie: str, signature: str) -> dict:
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
        json=chat_payload(),
        impersonate="chrome131",
        proxies={"http": PROXY, "https": PROXY},
        timeout=90,
    )
    body = resp.text[:2500]
    return {
        "http": resp.status_code,
        "cf": resp.headers.get("cf-mitigated"),
        "elapsed_s": round(time.time() - t0, 2),
        "sig_prefix": (signature or "")[:24],
        "sig_len": len(signature or ""),
        "has_pong": "PONG" in body,
        "has_token_frame": '"token"' in body or "messageTag" in body,
        "body_prefix": body[:500].replace("\n", " "),
    }


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else ".tmp/web-sso-canary.json")
    accounts = load_accounts(src)
    if not accounts:
        raise SystemExit("no accounts")
    acc = accounts[0]
    print(f"account_id={acc['id']} name={acc.get('name','')}")
    print("stage=bootstrap_sign")
    boot = bootstrap_sign(acc["sso"])
    print(
        json.dumps(
            {
                "title": boot.get("title"),
                "cookie_names": boot["cookie_names"],
                "network_caps": boot.get("network_caps"),
                "sig_len": len(boot["signature"] or ""),
                "sig_prefix": (boot["signature"] or "")[:24],
            },
            ensure_ascii=False,
        )
    )
    print("stage=pure_http_chat")
    result = pure_http_chat(boot["cookie"], boot["signature"])
    result["account_id"] = acc["id"]
    print(json.dumps(result, ensure_ascii=False))
    out = Path(".tmp/web-sign-then-http-result.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "bootstrap": {
                    "cookie_names": boot["cookie_names"],
                    "sig_len": len(boot["signature"]),
                    "network_caps": boot.get("network_caps"),
                },
                "chat": result,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("wrote", out)


if __name__ == "__main__":
    main()
