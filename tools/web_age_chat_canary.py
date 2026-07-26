#!/usr/bin/env python3
"""Dismiss age gate, send chat via UI, capture conversations/new sig, HTTP replay."""
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

STACK_HOOK = r"""
(() => {
  const orig = Headers.prototype.set;
  Headers.prototype.set = function(name, value) {
    try {
      if (String(name).toLowerCase() === 'x-statsig-id') {
        const stack = (new Error()).stack || '';
        document.documentElement.setAttribute('data-sig-stack', stack.slice(0, 1200));
      }
    } catch (_) {}
    return orig.apply(this, arguments);
  };
})();
"""


def load_sso(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return next(a for a in data["accounts"] if a.get("sso"))


def dismiss_age_gate(page) -> list[str]:
    notes: list[str] = []
    # Modal: 请确认你的年龄 / year input / 继续
    for text in ["请确认你的年龄", "选择你的出生年份", "Confirm your age"]:
        try:
            if page.get_by_text(text).count():
                notes.append(f"age-gate-seen:{text}")
                break
        except Exception:  # noqa: BLE001
            pass

    # Year field may already be 2000
    year_selectors = [
        'input[type="text"]',
        'input[type="number"]',
        'input',
    ]
    for sel in year_selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() == 0 or not loc.is_visible():
                continue
            loc.click(timeout=2000)
            loc.fill("2000")
            notes.append(f"year-filled:{sel}")
            break
        except Exception as e:  # noqa: BLE001
            notes.append(f"year-fail:{sel}:{type(e).__name__}")

    for label in ["继续", "Continue", "Confirm"]:
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count():
                btn.first.click(timeout=3000)
                notes.append(f"clicked:{label}")
                page.wait_for_timeout(3000)
                return notes
        except Exception as e:  # noqa: BLE001
            notes.append(f"btn-fail:{label}:{type(e).__name__}")

    # Fallback: black primary button
    try:
        page.locator("button").filter(has_text="继续").first.click(timeout=3000)
        notes.append("clicked:filter-继续")
        page.wait_for_timeout(3000)
    except Exception as e:  # noqa: BLE001
        notes.append(f"fallback-fail:{type(e).__name__}")
    return notes


def send_chat(page) -> list[str]:
    notes: list[str] = []
    box = page.locator('div[contenteditable="true"]').first
    try:
        box.click(timeout=5000)
        box.type("Reply with exactly: PONG", delay=12)
        notes.append("typed")
    except Exception as e:  # noqa: BLE001
        notes.append(f"type-fail:{type(e).__name__}")
        page.keyboard.type("Reply with exactly: PONG", delay=12)
        notes.append("keyboard-type")

    for key in ["Control+Enter", "Enter"]:
        page.keyboard.press(key)
        notes.append(f"key:{key}")
        page.wait_for_timeout(2000)

    # Send arrow button near composer
    for sel in [
        'button[aria-label*="Send" i]',
        'button[aria-label*="发送"]',
        'form button[type="submit"]',
        'button:right-of(div[contenteditable="true"])',
    ]:
        try:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible():
                btn.click(timeout=2000)
                notes.append(f"clicked-send:{sel}")
                break
        except Exception:  # noqa: BLE001
            pass
    return notes


def http_post(path: str, cookie: str, sig: str, payload: dict) -> dict:
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
    t0 = time.time()
    resp = crequests.post(
        "https://grok.com" + path,
        headers=headers,
        json=payload,
        impersonate="chrome131",
        proxies=PROXIES,
        timeout=90,
    )
    body = resp.text[:500]
    return {
        "path": path,
        "http": resp.status_code,
        "elapsed_s": round(time.time() - t0, 2),
        "sig_prefix": sig[:28],
        "has_pong": "PONG" in body,
        "has_token": '"token"' in body or "messageTag" in body,
        "body_prefix": body.replace("\n", " "),
        "okish": resp.status_code < 400,
    }


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else ".tmp/web-sso-canary.json")
    # Prefer account index if provided
    data = json.loads(src.read_text(encoding="utf-8"))
    idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    acc = data["accounts"][idx]
    print(f"account_id={acc['id']} idx={idx}")

    caps: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": PROXY},
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 900})
        context.add_init_script(STACK_HOOK)
        token = acc["sso"]
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
                }
            )

        page.on("request", on_req)
        page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(8000)

        age_notes = dismiss_age_gate(page)
        print("age", json.dumps(age_notes, ensure_ascii=False))
        page.screenshot(path=".tmp/web-after-age.png")

        send_notes = send_chat(page)
        print("send", json.dumps(send_notes, ensure_ascii=False))

        deadline = time.time() + 40
        while time.time() < deadline:
            if any(c["path"] == CHAT_PATH for c in caps):
                break
            page.wait_for_timeout(500)

        page.wait_for_timeout(4000)
        page.screenshot(path=".tmp/web-after-send.png")
        stack = page.locator("html").get_attribute("data-sig-stack") or ""
        cookies = context.cookies("https://grok.com/")
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        title = page.title()
        url = page.url
        browser.close()

    by_path = {c["path"]: c for c in caps}
    paths = sorted(by_path)
    print(
        json.dumps(
            {
                "title": title,
                "url": url,
                "paths": paths,
                "chat_new": CHAT_PATH in by_path,
                "stack_prefix": stack[:400].replace("\n", " | "),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    results = {}
    if RATE_PATH in by_path:
        results["rate"] = http_post(
            RATE_PATH,
            cookie,
            by_path[RATE_PATH]["sig"],
            {"requestKind": "DEFAULT", "modelName": "grok-3"},
        )
        print("rate", json.dumps(results["rate"], ensure_ascii=False))

    if CHAT_PATH in by_path:
        results["chat"] = http_post(CHAT_PATH, cookie, by_path[CHAT_PATH]["sig"], CHAT_PAYLOAD)
        print("chat", json.dumps(results["chat"], ensure_ascii=False))
    else:
        print("chat SKIP no conversations/new capture")

    verdict = {
        "age_dismissed": any("clicked" in n for n in age_notes),
        "ui_chat_new": CHAT_PATH in by_path,
        "rate_ok": bool(results.get("rate", {}).get("okish")),
        "chat_http_ok": bool(results.get("chat", {}).get("okish")),
        "chat_has_pong": bool(results.get("chat", {}).get("has_pong")),
        "pure_http_chat_ok": bool(
            results.get("chat", {}).get("okish")
            and (results.get("chat", {}).get("has_pong") or results.get("chat", {}).get("has_token"))
        ),
    }
    print("verdict", json.dumps(verdict, ensure_ascii=False))
    Path(".tmp/web-age-chat-result.json").write_text(
        json.dumps(
            {
                "account_id": acc["id"],
                "age_notes": age_notes,
                "send_notes": send_notes,
                "paths": paths,
                "stack": stack[:800],
                "results": results,
                "verdict": verdict,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("wrote .tmp/web-age-chat-result.json")


if __name__ == "__main__":
    main()
