#!/usr/bin/env python3
"""Focused Python repro: capture POST /conversations/new statsig, then curl_cffi chat+Lite."""
from __future__ import annotations

import importlib.util
import json
import sys
import time
import uuid
from pathlib import Path

from curl_cffi import requests as crequests
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / ".tmp"

spec = importlib.util.spec_from_file_location(
    "v1", ROOT / "tools" / "web_http_chat_image_canary.v1.py"
)
v1 = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(v1)

CHAT_PATH = v1.CHAT_PATH
CHAT_URL = v1.CHAT_URL
PROXY = v1.PROXY
PROXIES = v1.PROXIES
UA = v1.UA
IMPERSONATE = v1.IMPERSONATE
TURBOPACK_HOOK = v1.TURBOPACK_HOOK
CAPTURE_HOOK = v1.CAPTURE_HOOK
chat_payload = v1.chat_payload
cookie_header = v1.cookie_header
extract_image_urls = v1.extract_image_urls
classify_body = v1.classify_body


def post(cookie: str, statsig: str | None, payload: dict) -> dict:
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
        "user-agent": UA,
        "cookie": cookie,
        "x-xai-request-id": uuid.uuid4().hex,
    }
    if statsig:
        headers["x-statsig-id"] = statsig
    t0 = time.time()
    r = crequests.post(
        CHAT_URL,
        headers=headers,
        json=payload,
        impersonate=IMPERSONATE,
        proxies=PROXIES,
        timeout=120,
        stream=True,
    )
    chunks = []
    total = 0
    for chunk in r.iter_content(8192):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total >= 2_000_000:
            break
    body = b"".join(chunks).decode("utf-8", "replace")
    images = extract_image_urls(body)
    kind = classify_body(r.status_code, body, r.headers.get("cf-mitigated"))
    if images and r.status_code == 200 and payload.get("enableImageGeneration"):
        kind = "image_ok"
    return {
        "http": r.status_code,
        "kind": kind,
        "elapsed_s": round(time.time() - t0, 2),
        "has_token_hint": ("token" in body.lower()) or ("PONG" in body),
        "image_count": len(images),
        "image_urls_prefix": [u[:120] for u in images[:3]],
        "body_prefix": body[:240].replace("\n", " "),
    }


def capture_new_chat_sig(sso: str) -> dict:
    token = sso.strip()
    if token.lower().startswith("sso="):
        token = token[4:].strip()
    out = {"statsigId": None, "cookie": None, "source": None, "notes": [], "method": None}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": PROXY},
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 900})
        context.add_init_script(TURBOPACK_HOOK)
        context.add_init_script(CAPTURE_HOOK)
        context.add_cookies(
            [
                {"name": "sso", "value": token, "domain": ".grok.com", "path": "/"},
                {"name": "sso-rw", "value": token, "domain": ".grok.com", "path": "/"},
            ]
        )
        page = context.new_page()
        posts: list[dict] = []

        def on_request(req):
            if req.method.upper() != "POST":
                return
            if "/rest/app-chat/conversations/new" not in req.url:
                return
            sig = req.headers.get("x-statsig-id") or ""
            if sig:
                posts.append({"sig": sig, "url": req.url})

        page.on("request", on_request)
        page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=90000)
        try:
            page.wait_for_load_state("networkidle", timeout=25000)
        except Exception:
            page.wait_for_timeout(8000)
        out["notes"].append(f"url={page.url}")

        # Dismiss birth-year / age confirmation modal if present (blocks chat send).
        try:
            page.wait_for_selector("text=出生年份", timeout=8000)
            out["notes"].append("age_modal:seen")
        except Exception:
            out["notes"].append("age_modal:absent")
        for clicker in (
            lambda: page.get_by_role("button", name="保存").click(timeout=3000),
            lambda: page.get_by_role("button", name="Save").click(timeout=3000),
            lambda: page.locator("button").filter(has_text="保存").last.click(timeout=3000),
            lambda: page.locator("text=保存").last.click(timeout=3000),
        ):
            try:
                clicker()
                out["notes"].append("age_modal:saved")
                page.wait_for_timeout(2500)
                break
            except Exception as exc:  # noqa: BLE001
                out["notes"].append(f"dismiss_try:{type(exc).__name__}")

        # Try turbopack sign for exact path first
        try:
            signed = page.evaluate(
                """async ({path}) => {
                  const sleep = ms => new Promise(r => setTimeout(r, ms));
                  for (let i = 0; i < 40; i++) {
                    if (globalThis.__grokBridgeRuntime && document.body && document.body.childNodes.length >= 3) break;
                    await sleep(500);
                  }
                  if (!globalThis.__grokBridgeRuntime) return {error:'no runtime'};
                  await sleep(2000);
                  const runtime = globalThis.__grokBridgeRuntime;
                  const cache = runtime.c || runtime.m || {};
                  const ids = [4629918, ...Object.keys(cache).map(Number).filter(n => n > 0)].slice(0, 60);
                  const errs = [];
                  for (const id of ids) {
                    try {
                      const mod = await runtime.A(id);
                      if (!mod || typeof mod.default !== 'function') continue;
                      const signer = mod.default();
                      const sig = String(await signer(path, 'POST') || '');
                      if (sig.length > 20 && !sig.startsWith('x0:')) return {statsigId: sig, moduleId: id};
                    } catch (e) { errs.push(String(id)+':'+String(e&&e.message||e).slice(0,60)); }
                  }
                  return {error:'no module', errs: errs.slice(0,8)};
                }""",
                {"path": CHAT_PATH},
            )
            out["notes"].append(f"turbopack={signed}")
            if isinstance(signed, dict) and signed.get("statsigId"):
                out["statsigId"] = signed["statsigId"]
                out["source"] = f"turbopack:{signed.get('moduleId')}"
        except Exception as exc:  # noqa: BLE001
            out["notes"].append(f"turbopack_exc:{type(exc).__name__}")

        if not out["statsigId"]:
            # Drive UI: expect POST conversations/new
            try:
                for sel in ('div[role="textbox"]', "[contenteditable=true]", "textarea"):
                    loc = page.locator(sel).first
                    if loc.count() == 0:
                        continue
                    loc.click(timeout=5000)
                    loc.fill("Reply with exactly: PONG")
                    out["notes"].append(f"filled:{sel}")
                    break
                send_selectors = [
                    'button[aria-label*="Send" i]',
                    'button[type="submit"]',
                    'button:has-text("Send")',
                ]
                clicked = False
                for sel in send_selectors:
                    btn = page.locator(sel).first
                    try:
                        if btn.count() == 0:
                            continue
                        with page.expect_request(
                            lambda r: r.method == "POST"
                            and "/rest/app-chat/conversations/new" in r.url
                            and bool(r.headers.get("x-statsig-id")),
                            timeout=45000,
                        ) as req_info:
                            btn.click(timeout=5000)
                        req = req_info.value
                        out["statsigId"] = req.headers.get("x-statsig-id")
                        out["source"] = "ui_post:conversations/new"
                        out["method"] = "POST"
                        out["notes"].append(f"send:{sel}")
                        clicked = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        out["notes"].append(f"send_try:{sel}:{type(exc).__name__}")
                if not clicked:
                    with page.expect_request(
                        lambda r: r.method == "POST"
                        and "/rest/app-chat/conversations/new" in r.url
                        and bool(r.headers.get("x-statsig-id")),
                        timeout=45000,
                    ) as req_info:
                        page.keyboard.press("Enter")
                    req = req_info.value
                    out["statsigId"] = req.headers.get("x-statsig-id")
                    out["source"] = "ui_enter:conversations/new"
                    out["notes"].append("enter")
            except Exception as exc:  # noqa: BLE001
                out["notes"].append(f"ui:{type(exc).__name__}:{str(exc)[:100]}")
                if posts:
                    out["statsigId"] = posts[-1]["sig"]
                    out["source"] = "net_post:conversations/new"
                    out["notes"].append(f"fallback_posts={len(posts)}")

        cookies = context.cookies("https://grok.com/")
        out["cookie"] = "; ".join(f'{c["name"]}={c["value"]}' for c in cookies)
        out["cookie_names"] = sorted({c["name"] for c in cookies})
        try:
            TMP.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(TMP / "http-repro-sign.png"))
        except Exception:
            pass
        browser.close()
    return out


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else ".tmp/web-sso-canary.json")
    accounts = v1.load_accounts(src)
    aid = int(sys.argv[2]) if len(sys.argv) > 2 else accounts[0]["id"]
    acc = next(a for a in accounts if a["id"] == aid)
    print(json.dumps({"event": "start", "account_id": aid}))

    a = post(cookie_header(acc["sso"]), None, chat_payload("Reply with exactly: PONG"))
    print(json.dumps({"event": "A_no_sig", "http": a["http"], "kind": a["kind"]}))

    cap = capture_new_chat_sig(acc["sso"])
    print(
        json.dumps(
            {
                "event": "B_sign",
                "source": cap.get("source"),
                "has_statsig": bool(cap.get("statsigId")),
                "statsig_len": len(cap.get("statsigId") or ""),
                "notes": (cap.get("notes") or [])[:15],
            },
            ensure_ascii=False,
        )
    )
    if not cap.get("statsigId"):
        print(json.dumps({"event": "fail", "reason": "no POST /conversations/new sig"}))
        raise SystemExit(2)

    cookie = cap.get("cookie") or cookie_header(acc["sso"])
    c = post(cookie, cap["statsigId"], chat_payload("Reply with exactly: PONG"))
    print(
        json.dumps(
            {
                "event": "C_signed_chat",
                "http": c["http"],
                "kind": c["kind"],
                "has_token_hint": c["has_token_hint"],
                "elapsed_s": c["elapsed_s"],
                "body_prefix": c["body_prefix"][:160],
            },
            ensure_ascii=False,
        )
    )

    # Fresh sign for Lite
    cap2 = capture_new_chat_sig(acc["sso"])
    image_sig = cap2.get("statsigId") or cap["statsigId"]
    cookie2 = cap2.get("cookie") or cookie
    d = post(
        cookie2,
        image_sig,
        chat_payload("Drawing: a simple red apple on white background, minimal", enable_image=True),
    )
    print(
        json.dumps(
            {
                "event": "D_lite_image",
                "http": d["http"],
                "kind": d["kind"],
                "image_count": d["image_count"],
                "image_urls_prefix": d["image_urls_prefix"],
                "elapsed_s": d["elapsed_s"],
            },
            ensure_ascii=False,
        )
    )

    ok = a["kind"] == "anti_bot_403" and c["kind"] == "chat_ok" and (
        d["kind"] == "image_ok" or d["image_count"] > 0
    )
    out = {
        "A": a,
        "B": {"source": cap.get("source"), "statsig_len": len(cap.get("statsigId") or ""), "notes": cap.get("notes")},
        "C": c,
        "D": d,
        "acceptance": {"A_anti_bot": a["kind"] == "anti_bot_403", "C_chat_ok": c["kind"] == "chat_ok", "D_image_ok": d["kind"] == "image_ok" or d["image_count"] > 0},
    }
    TMP.mkdir(parents=True, exist_ok=True)
    path = TMP / "web-http-repro-result.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"event": "acceptance", **out["acceptance"], "path": str(path)}))
    raise SystemExit(0 if ok else 2)


if __name__ == "__main__":
    main()
