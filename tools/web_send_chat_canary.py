#!/usr/bin/env python3
"""Type into composer, click Send when it appears, capture chat sig, curl_cffi replay."""
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


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else ".tmp/web-sso-canary.json")
    idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    acc = json.loads(src.read_text(encoding="utf-8"))["accounts"][idx]
    print(f"account_id={acc['id']}")

    caps: list[dict] = []
    notes: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": PROXY},
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 900})
        context.add_cookies(
            [
                {"name": "sso", "value": acc["sso"], "domain": ".grok.com", "path": "/"},
                {"name": "sso-rw", "value": acc["sso"], "domain": ".grok.com", "path": "/"},
            ]
        )
        page = context.new_page()

        def on_req(req):
            if "/rest/" not in req.url:
                return
            sig = req.headers.get("x-statsig-id") or ""
            if sig:
                caps.append({"path": urlparse(req.url).path, "method": req.method, "sig": sig})

        page.on("request", on_req)
        page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(10000)

        # Dismiss age gate if present
        try:
            if page.get_by_text("请确认你的年龄").count():
                page.locator("input").first.fill("2000")
                page.get_by_role("button", name="继续").click()
                notes.append("age-dismissed")
                page.wait_for_timeout(3000)
        except Exception as e:  # noqa: BLE001
            notes.append(f"age-skip:{type(e).__name__}")

        # Dismiss X link banner
        try:
            ign = page.get_by_role("button", name="忽略")
            if ign.count() and ign.first.is_visible():
                ign.first.click()
                notes.append("ignored-x-banner")
                page.wait_for_timeout(1000)
        except Exception:  # noqa: BLE001
            pass

        # Focus composer
        composer = page.get_by_role("textbox", name="Ask Grok anything")
        if composer.count() == 0:
            composer = page.locator('[contenteditable="true"][aria-label*="Ask"], [contenteditable="true"]')
        composer.first.click(timeout=5000)
        composer.first.type("Reply with exactly: PONG", delay=15)
        notes.append("typed")
        page.wait_for_timeout(1500)

        # Send button should appear after text
        send_clicked = False
        for sel in [
            'button[aria-label*="发送"]',
            'button[aria-label*="Send" i]',
            'button[type="submit"]',
        ]:
            try:
                btn = page.locator(sel).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=3000)
                    notes.append(f"send:{sel}")
                    send_clicked = True
                    break
            except Exception as e:  # noqa: BLE001
                notes.append(f"send-fail:{sel}:{type(e).__name__}")

        if not send_clicked:
            # Last visible button near composer area — often the arrow
            try:
                # buttons that appeared after typing
                candidates = page.locator("button:visible").all()
                for btn in reversed(candidates[-8:]):
                    aria = (btn.get_attribute("aria-label") or "").lower()
                    txt = (btn.inner_text() or "").strip()
                    if any(k in aria for k in ("send", "提交", "发送")) or txt in {"", "↑"}:
                        # skip known non-send
                        if any(k in aria for k in ("dictat", "voice", "听写", "语音", "附件", "model", "模型")):
                            continue
                        btn.click(timeout=2000)
                        notes.append(f"send-candidate:aria={aria!r}:txt={txt!r}")
                        send_clicked = True
                        break
            except Exception as e:  # noqa: BLE001
                notes.append(f"candidate-fail:{type(e).__name__}")

        if not send_clicked:
            page.keyboard.press("Control+Enter")
            notes.append("ctrl-enter-fallback")

        deadline = time.time() + 50
        while time.time() < deadline:
            if any(c["path"] == CHAT_PATH for c in caps):
                notes.append("got-chat-new")
                break
            page.wait_for_timeout(400)

        page.wait_for_timeout(5000)
        page.screenshot(path=".tmp/web-send-attempt.png")

        # Dump visible buttons after typing for debug
        btn_dump = []
        for btn in page.locator("button:visible").all()[:30]:
            try:
                btn_dump.append(
                    {
                        "aria": btn.get_attribute("aria-label"),
                        "text": (btn.inner_text() or "")[:30],
                    }
                )
            except Exception:  # noqa: BLE001
                pass

        cookies = context.cookies("https://grok.com/")
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        browser.close()

    by = {c["path"]: c for c in caps}
    print(json.dumps({"notes": notes, "paths": sorted(by), "chat_new": CHAT_PATH in by, "buttons": btn_dump}, ensure_ascii=False, indent=2))

    results = {}
    if RATE_PATH in by:
        r = crequests.post(
            "https://grok.com" + RATE_PATH,
            headers={
                "content-type": "application/json",
                "origin": "https://grok.com",
                "referer": "https://grok.com/",
                "user-agent": UA,
                "cookie": cookie,
                "x-statsig-id": by[RATE_PATH]["sig"],
            },
            json={"requestKind": "DEFAULT", "modelName": "grok-3"},
            impersonate="chrome131",
            proxies=PROXIES,
            timeout=60,
        )
        results["rate"] = {"http": r.status_code, "body": r.text[:200]}
        print("rate", results["rate"])

    if CHAT_PATH in by:
        t0 = time.time()
        r = crequests.post(
            "https://grok.com" + CHAT_PATH,
            headers={
                "accept": "*/*",
                "content-type": "application/json",
                "origin": "https://grok.com",
                "referer": "https://grok.com/",
                "user-agent": UA,
                "cookie": cookie,
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "x-statsig-id": by[CHAT_PATH]["sig"],
            },
            json=CHAT_PAYLOAD,
            impersonate="chrome131",
            proxies=PROXIES,
            timeout=90,
        )
        body = r.text[:500]
        results["chat"] = {
            "http": r.status_code,
            "elapsed_s": round(time.time() - t0, 2),
            "sig_prefix": by[CHAT_PATH]["sig"][:28],
            "has_pong": "PONG" in body,
            "has_token": '"token"' in body or "messageTag" in body,
            "body_prefix": body.replace("\n", " "),
        }
        print("chat", json.dumps(results["chat"], ensure_ascii=False))
    else:
        print("chat SKIP")

    verdict = {
        "ui_chat_new": CHAT_PATH in by,
        "pure_http_chat_ok": bool(
            results.get("chat")
            and results["chat"]["http"] < 400
            and (results["chat"]["has_pong"] or results["chat"]["has_token"])
        ),
        "chat_http": (results.get("chat") or {}).get("http"),
    }
    print("verdict", json.dumps(verdict, ensure_ascii=False))
    Path(".tmp/web-send-chat-result.json").write_text(
        json.dumps({"account_id": acc["id"], "notes": notes, "results": results, "verdict": verdict, "paths": sorted(by)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
