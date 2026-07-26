#!/usr/bin/env python3
"""Capture SPA-native x-statsig-id by driving the real chat UI, then pure HTTP replay."""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

from curl_cffi import requests as crequests
from playwright.sync_api import sync_playwright

PROXY = "http://127.0.0.1:7897"
PROXIES = {"http": PROXY, "https": PROXY}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
CHAT_URL = "https://grok.com/rest/app-chat/conversations/new"
RATE_URL = "https://grok.com/rest/rate-limits"

CAPTURE_HOOK = r"""
(() => {
  const captured = [];
  globalThis.__grokCapturedSigs = captured;
  const grab = (headers, url, method) => {
    if (!headers) return;
    let sig = '';
    try {
      if (typeof Headers !== 'undefined' && headers instanceof Headers) sig = headers.get('x-statsig-id') || '';
      else if (Array.isArray(headers)) {
        const hit = headers.find(h => String(h[0]).toLowerCase() === 'x-statsig-id');
        sig = hit ? String(hit[1]) : '';
      } else if (typeof headers === 'object') {
        for (const [k, v] of Object.entries(headers)) {
          if (String(k).toLowerCase() === 'x-statsig-id') { sig = String(v); break; }
        }
      }
    } catch (_) {}
    if (!sig) return;
    try {
      const u = new URL(String(url), location.origin);
      if (!u.pathname.startsWith('/rest/')) return;
      captured.push({path: u.pathname, method: String(method || 'GET').toUpperCase(), sig, t: Date.now()});
    } catch (_) {}
  };
  const origFetch = window.fetch;
  window.fetch = function(input, init = {}) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = (init && init.method) || (typeof input !== 'string' && input && input.method) || 'GET';
    grab(init && init.headers, url, method);
    if (typeof Request !== 'undefined' && input instanceof Request) grab(input.headers, input.url, input.method);
    return origFetch.apply(this, arguments);
  };
  const origOpen = XMLHttpRequest.prototype.open;
  const origSet = XMLHttpRequest.prototype.setRequestHeader;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__grokM = method; this.__grokU = url; this.__grokH = {};
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function(k, v) {
    this.__grokH[k] = v;
    return origSet.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function() {
    grab(this.__grokH, this.__grokU, this.__grokM);
    return origSend.apply(this, arguments);
  };
})();
"""


def chat_payload() -> dict:
    return {
        "temporary": True,
        "modelName": "grok-3",
        "message": "Reply with exactly: PONG",
        "fileAttachments": [],
        "imageAttachments": [],
        "disableSearch": True,
        "enableImageGeneration": False,
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "enableImageStreaming": False,
        "imageGenerationCount": 0,
        "forceConcise": True,
        "toolOverrides": {},
        "enableSideBySide": True,
        "sendFinalMetadata": True,
        "isPreset": False,
        "disableTextFollowUps": True,
    }


def chat_payload_v2() -> dict:
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


def drive_and_capture(sso: str) -> dict:
    out = {
        "cookie": "",
        "cookie_names": [],
        "captured": [],
        "ui_notes": [],
        "page_url": "",
        "script_hints": [],
    }
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": PROXY},
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 900})
        context.add_init_script(CAPTURE_HOOK)
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

        # Also capture via Playwright request events (more reliable)
        net_caps = []

        def on_request(req):
            if "/rest/" not in req.url:
                return
            sig = req.headers.get("x-statsig-id") or ""
            if sig:
                from urllib.parse import urlparse

                net_caps.append(
                    {
                        "path": urlparse(req.url).path,
                        "method": req.method,
                        "sig": sig,
                        "src": "pw",
                    }
                )

        page.on("request", on_request)
        page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(8000)
        out["page_url"] = page.url

        # Scan loaded scripts for statsig strings
        hints = page.evaluate(
            """async () => {
              const out = [];
              const urls = performance.getEntriesByType('resource')
                .map(e => e.name)
                .filter(u => /\\.m?js(\\?|$)/.test(u) && /grok|cdn|_next/.test(u))
                .slice(0, 60);
              for (const url of urls) {
                try {
                  const txt = await (await fetch(url)).text();
                  if (/x-statsig-id|statsig/.test(txt)) {
                    const i = txt.indexOf('x-statsig-id');
                    const j = txt.search(/statsig/i);
                    out.push({
                      url: url.slice(-100),
                      hasHeader: i >= 0,
                      snippet: txt.slice(Math.max(0, (i >= 0 ? i : j) - 60), (i >= 0 ? i : j) + 140).replace(/\\s+/g, ' '),
                    });
                  }
                } catch (e) {}
              }
              return {urls: urls.length, hints: out.slice(0, 15)};
            }"""
        )
        out["script_hints"] = hints

        # Try find composer and send a short message
        selectors = [
            'textarea',
            '[contenteditable="true"]',
            'div[role="textbox"]',
            'textarea[placeholder]',
        ]
        filled = False
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() == 0:
                    continue
                loc.click(timeout=3000)
                loc.fill("Reply with exactly: PONG")
                filled = True
                out["ui_notes"].append(f"filled:{sel}")
                break
            except Exception as e:
                out["ui_notes"].append(f"fail:{sel}:{type(e).__name__}")
        if filled:
            page.keyboard.press("Enter")
            out["ui_notes"].append("pressed Enter")
            page.wait_for_timeout(12000)
        else:
            # fallback: click any send-like button after typing via keyboard focus
            page.keyboard.type("Reply with exactly: PONG", delay=20)
            page.keyboard.press("Enter")
            out["ui_notes"].append("keyboard fallback")
            page.wait_for_timeout(12000)

        js_caps = page.evaluate("() => (globalThis.__grokCapturedSigs || []).slice(-20)")
        # merge without full sig duplication in notes
        merged = []
        seen = set()
        for item in list(js_caps or []) + net_caps:
            key = (item.get("path"), item.get("method"), item.get("sig", "")[:24])
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        out["captured"] = merged

        cookies = context.cookies("https://grok.com/")
        out["cookie_names"] = sorted({c["name"] for c in cookies})
        out["cookie"] = "; ".join(f'{c["name"]}={c["value"]}' for c in cookies)
        # screenshot for debug
        try:
            page.screenshot(path=".tmp/grok-ui-capture.png", full_page=False)
            out["ui_notes"].append("screenshot:.tmp/grok-ui-capture.png")
        except Exception:
            pass
        browser.close()
    return out


def http_post(url: str, cookie: str, statsig: str, payload: dict) -> dict:
    headers = {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
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
        "x-xai-request-id": uuid.uuid4().hex,
    }
    if statsig:
        headers["x-statsig-id"] = statsig
    t0 = time.time()
    r = crequests.post(url, headers=headers, json=payload, impersonate="chrome131", proxies=PROXIES, timeout=90)
    return {
        "http": r.status_code,
        "elapsed_s": round(time.time() - t0, 2),
        "has_statsig": bool(statsig),
        "body_prefix": r.text[:400].replace("\n", " "),
        "ok": r.status_code == 200,
    }


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else ".tmp/web-sso-canary.json")
    data = json.loads(src.read_text(encoding="utf-8"))
    acc = data["accounts"][0]
    print(f"account_id={acc['id']}")
    print("=== drive UI + capture statsig ===")
    cap = drive_and_capture(acc["sso"])
    safe_caps = [
        {
            "path": c.get("path"),
            "method": c.get("method"),
            "sig_len": len(c.get("sig") or ""),
            "sig_prefix": (c.get("sig") or "")[:28],
            "src": c.get("src", "js"),
        }
        for c in cap["captured"]
    ]
    print(json.dumps({
        "page_url": cap["page_url"],
        "cookie_names": cap["cookie_names"],
        "ui_notes": cap["ui_notes"],
        "script_hints": cap["script_hints"],
        "captured": safe_caps,
    }, ensure_ascii=False, indent=2))

    chat_sig = ""
    rate_sig = ""
    for c in cap["captured"]:
        path = c.get("path") or ""
        if "conversations" in path and c.get("sig"):
            chat_sig = c["sig"]
        if "rate-limits" in path and c.get("sig"):
            rate_sig = c["sig"]
    if not chat_sig and cap["captured"]:
        # any rest sig as last resort for chat path test
        chat_sig = cap["captured"][-1].get("sig") or ""

    print("=== pure HTTP replay ===")
    rate = http_post(RATE_URL, cap["cookie"], rate_sig, {"modelName": "fast"})
    print("rate", json.dumps(rate, ensure_ascii=False))
    chat = http_post(CHAT_URL, cap["cookie"], chat_sig, chat_payload_v2())
    print("chat", json.dumps(chat, ensure_ascii=False))

    out = {
        "account_id": acc["id"],
        "captured": safe_caps,
        "ui_notes": cap["ui_notes"],
        "script_hints": cap["script_hints"],
        "http_rate": rate,
        "http_chat": chat,
        "used_chat_sig_len": len(chat_sig),
        "used_rate_sig_len": len(rate_sig),
    }
    Path(".tmp/web-ui-sign-capture-result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("wrote .tmp/web-ui-sign-capture-result.json")


if __name__ == "__main__":
    main()
