#!/usr/bin/env python3
"""Drive real Grok UI to capture conversations/new Statsig, then HTTP replay + path controls."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from curl_cffi import requests as crequests
from playwright.sync_api import sync_playwright

PROXY = "http://127.0.0.1:7897"
PROXIES = {"http": PROXY, "https": PROXY}
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CHAT_PATH = "/rest/app-chat/conversations/new"
RATE_PATH = "/rest/rate-limits"

CHAT_PAYLOAD = {
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


def load_sso(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return next(a for a in data["accounts"] if a.get("sso"))


def drive_ui(sso: str) -> dict:
    token = sso.strip()
    if token.lower().startswith("sso="):
        token = token[4:].strip()

    caps: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": PROXY},
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 900})
        context.add_cookies(
            [
                {"name": "sso", "value": token, "domain": ".grok.com", "path": "/"},
                {"name": "sso-rw", "value": token, "domain": ".grok.com", "path": "/"},
            ]
        )
        page = context.new_page()

        def on_req(req):
            if "/rest/" not in req.url:
                return
            sig = req.headers.get("x-statsig-id") or ""
            if not sig:
                return
            caps.append(
                {
                    "path": urlparse(req.url).path,
                    "method": req.method,
                    "sig": sig,
                    "t": time.time(),
                }
            )

        page.on("request", on_req)
        page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(10000)

        notes: list[str] = []
        # Prefer visible composer
        selectors = [
            'div[contenteditable="true"]',
            'textarea',
            '[role="textbox"]',
            'div.tiptap',
            '[data-testid*="chat"] textarea',
            '[data-testid*="composer"]',
        ]
        filled = False
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() == 0:
                    continue
                loc.click(timeout=4000)
                # contenteditable often rejects fill(); use type
                try:
                    loc.fill("Reply with exactly: PONG")
                except Exception:
                    loc.type("Reply with exactly: PONG", delay=15)
                filled = True
                notes.append(f"filled:{sel}")
                break
            except Exception as e:  # noqa: BLE001
                notes.append(f"fail:{sel}:{type(e).__name__}")

        if not filled:
            page.keyboard.type("Reply with exactly: PONG", delay=20)
            notes.append("keyboard-type-fallback")

        # Try Enter and/or Send button
        page.keyboard.press("Enter")
        notes.append("enter")
        page.wait_for_timeout(2000)
        for btn_sel in [
            'button[aria-label*="Send"]',
            'button[type="submit"]',
            'button:has-text("Send")',
            '[data-testid*="send"]',
        ]:
            try:
                btn = page.locator(btn_sel).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=2000)
                    notes.append(f"clicked:{btn_sel}")
                    break
            except Exception:  # noqa: BLE001
                pass

        # Wait for chat request
        deadline = time.time() + 45
        while time.time() < deadline:
            if any(CHAT_PATH in c["path"] for c in caps):
                notes.append("captured-chat-new")
                break
            page.wait_for_timeout(500)

        page.wait_for_timeout(5000)
        cookies = context.cookies("https://grok.com/")
        try:
            page.screenshot(path=".tmp/web-ui-drive.png")
            notes.append("screenshot")
        except Exception:  # noqa: BLE001
            pass
        title = page.title()
        url = page.url
        browser.close()

    by_path: dict[str, dict] = {}
    for c in caps:
        by_path[c["path"]] = c  # last wins

    return {
        "title": title,
        "url": url,
        "notes": notes,
        "cookie": "; ".join(f"{c['name']}={c['value']}" for c in cookies),
        "cookie_names": sorted({c["name"] for c in cookies}),
        "caps": [
            {"path": c["path"], "method": c["method"], "sig_len": len(c["sig"]), "sig_prefix": c["sig"][:28]}
            for c in caps
        ],
        "by_path": by_path,
        "paths": sorted(by_path.keys()),
    }


def http_call(method: str, path: str, cookie: str, sig: str, payload=None) -> dict:
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
        "user-agent": UA,
        "cookie": cookie,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-statsig-id": sig,
    }
    url = "https://grok.com" + path
    t0 = time.time()
    if method.upper() == "GET":
        resp = crequests.get(url, headers=headers, impersonate="chrome131", proxies=PROXIES, timeout=60)
    else:
        resp = crequests.post(
            url,
            headers=headers,
            json=payload if payload is not None else {},
            impersonate="chrome131",
            proxies=PROXIES,
            timeout=90,
        )
    body = resp.text[:400]
    return {
        "method": method.upper(),
        "path": path,
        "http": resp.status_code,
        "elapsed_s": round(time.time() - t0, 2),
        "cf": resp.headers.get("cf-mitigated"),
        "sig_prefix": sig[:28],
        "body_prefix": body.replace("\n", " "),
        "okish": resp.status_code < 400,
    }


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else ".tmp/web-sso-canary.json")
    acc = load_sso(src)
    print(f"account_id={acc['id']}")
    print("=== drive UI ===")
    boot = drive_ui(acc["sso"])
    print(
        json.dumps(
            {
                "title": boot["title"],
                "url": boot["url"],
                "notes": boot["notes"],
                "cookie_names": boot["cookie_names"],
                "paths": boot["paths"],
                "caps": boot["caps"][-15:],
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    by = boot["by_path"]
    chat = by.get(CHAT_PATH)
    rate = by.get(RATE_PATH)
    conv_list = by.get("/rest/app-chat/conversations")

    print("=== HTTP replays ===")
    results = {}

    # Control: path-matched rate-limits
    if rate:
        results["rate_matched"] = http_call("POST", RATE_PATH, boot["cookie"], rate["sig"], {"requestKind": "DEFAULT", "modelName": "grok-3"})
        print("rate_matched", json.dumps(results["rate_matched"], ensure_ascii=False))
    else:
        print("rate_matched SKIP no sig")

    # Control: wrong-path sig on chat
    if rate and not chat:
        results["chat_with_rate_sig"] = http_call("POST", CHAT_PATH, boot["cookie"], rate["sig"], CHAT_PAYLOAD)
        print("chat_with_rate_sig", json.dumps(results["chat_with_rate_sig"], ensure_ascii=False))

    if conv_list and not chat:
        results["chat_with_convlist_sig"] = http_call("POST", CHAT_PATH, boot["cookie"], conv_list["sig"], CHAT_PAYLOAD)
        print("chat_with_convlist_sig", json.dumps(results["chat_with_convlist_sig"], ensure_ascii=False))

    # Target: path-correct chat
    if chat:
        results["chat_matched"] = http_call("POST", CHAT_PATH, boot["cookie"], chat["sig"], CHAT_PAYLOAD)
        print("chat_matched", json.dumps(results["chat_matched"], ensure_ascii=False))
    else:
        print("chat_matched SKIP — UI did not fire conversations/new")

    verdict = {
        "ui_fired_chat_new": bool(chat),
        "rate_http_ok": bool(results.get("rate_matched", {}).get("okish")),
        "chat_matched_ok": bool(results.get("chat_matched", {}).get("okish")),
        "chat_matched_http": results.get("chat_matched", {}).get("http"),
        "chat_wrong_sig_http": (results.get("chat_with_rate_sig") or results.get("chat_with_convlist_sig") or {}).get("http"),
    }
    print("verdict", json.dumps(verdict, ensure_ascii=False))
    out = {
        "account_id": acc["id"],
        "notes": boot["notes"],
        "paths": boot["paths"],
        "caps": boot["caps"],
        "results": results,
        "verdict": verdict,
        "had_chat_sig": bool(chat),
        "had_rate_sig": bool(rate),
    }
    Path(".tmp/web-ui-drive-result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("wrote .tmp/web-ui-drive-result.json")


if __name__ == "__main__":
    main()
