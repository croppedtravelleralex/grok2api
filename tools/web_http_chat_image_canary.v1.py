#!/usr/bin/env python3
"""FROZEN baseline — local Mihomo HTTP chat + Lite image (2026-07-18).

Canonical chain doc: docs/http-reverse-lite-chain.md
Entry pointer: tools/web_http_chat_image_canary.py → this file
Acceptance snapshot: .tmp/web-http-chat-image-canary-baseline.json

Frozen chain (do not evolve business logic here):
  A) POST /rest/app-chat/conversations/new 无 x-statsig-id → anti-bot 403
  B) 本机 Playwright 短签（Turbopack / UI capture；可选 panda /v1/sign）
  C) 同 path + SSO cookie + x-statsig-id → curl_cffi chat 200
  D) message=`Drawing: …` + enableImageGeneration → Lite 图路径（assets.grok.com）

Secrets never printed; results under .tmp/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from curl_cffi import requests as crequests

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / ".tmp"
PROXY = "http://127.0.0.1:7897"
PROXIES = {"http": PROXY, "https": PROXY}
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
IMPERSONATE = "chrome131"
CHAT_PATH = "/rest/app-chat/conversations/new"
CHAT_URL = "https://grok.com" + CHAT_PATH
SIGNER_MODULE_CANDIDATES = (4629918, 4629917, 4629919, 4629920, 4630000, 4610000)

TURBOPACK_HOOK = r"""
(() => {
  const queue = [];
  const nativePush = Array.prototype.push;
  const wrap = entry => {
    if (!Array.isArray(entry) || typeof entry[entry.length - 1] !== 'function') return entry;
    const original = entry[entry.length - 1];
    entry[entry.length - 1] = function(...args) {
      if (args[0]) globalThis.__grokBridgeRuntime = args[0];
      return original.apply(this, args);
    };
    return entry;
  };
  queue.push = function(...entries) { return nativePush.apply(this, entries.map(wrap)); };
  globalThis.TURBOPACK = queue;
})();
"""

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
})();
"""


def cookie_header(sso: str) -> str:
    token = sso.strip()
    if token.lower().startswith("sso="):
        token = token[4:].strip()
    if ";" in token:
        token = token.split(";", 1)[0].strip()
    return f"sso={token}; sso-rw={token}"


def _dismiss_age_gate(page) -> list[str]:
    """Dismiss birth-year modal only when actually present. Never click cookie banners."""
    notes: list[str] = []
    # X-connect / promo cards: click 忽略 so composer stays usable (fully automated).
    for label in ("忽略", "Dismiss", "Not now"):
        try:
            loc = page.get_by_role("button", name=label)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=2000, force=True)
                notes.append(f"promo-dismiss:{label}")
                page.wait_for_timeout(800)
        except Exception:
            pass

    present = False
    for text in (
        "出生年份",
        "请确认您的出生年份",
        "之后您将无法更改",
        "之后无法更改",
        "Birth year",
        "confirm your birth",
    ):
        try:
            if page.get_by_text(text, exact=False).count():
                notes.append(f"age-seen:{text}")
                present = True
                break
        except Exception:
            pass
    if not present:
        try:
            if page.get_by_role("button", name=re.compile(r"^保存$|^Save$")).count():
                notes.append("age-seen:save-button")
                present = True
        except Exception:
            pass
    if not present:
        notes.append("age-absent")
        return notes

    for name in ("保存", "Save"):
        try:
            loc = page.get_by_role("button", name=name)
            if loc.count():
                loc.first.click(timeout=4000, force=True)
                notes.append(f"age-click:{name}")
                page.wait_for_timeout(2000)
                return notes
        except Exception as exc:  # noqa: BLE001
            notes.append(f"age-fail:{name}:{type(exc).__name__}")
    try:
        page.locator("button").filter(has_text=re.compile(r"^保存$")).last.click(timeout=3000, force=True)
        notes.append("age-click:filter-保存")
        page.wait_for_timeout(2000)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"age-filter-fail:{type(exc).__name__}")
    return notes


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
        "disableTextFollowUps": False,
        "enableImageGeneration": enable_image,
        "enableImageStreaming": enable_image,
        "enableSideBySide": True,
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


def classify_body(status: int, body: str, cf: str | None) -> str:
    lower = body.lower()
    if cf or "just a moment" in lower:
        return "cf_challenge"
    if status == 403 and "anti-bot" in lower:
        return "anti_bot_403"
    if status == 401:
        return "auth_401"
    if status == 200 and (("PONG" in body) or ("token" in lower) or ('"message"' in lower)):
        return "chat_ok"
    if status == 200 and extract_image_urls(body):
        return "image_ok"
    if status == 200:
        return "http_200_unclear"
    return f"http_{status}"


def extract_image_urls(body: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        url = (url or "").strip().rstrip("\\")
        if not url or url in seen:
            return
        if url.startswith("//"):
            url = "https:" + url
        if not (url.startswith("http://") or url.startswith("https://") or url.startswith("users/") or "/generated/" in url):
            return
        seen.add(url)
        found.append(url)

    for m in re.finditer(r"https?://[^\s\"'<>\\]+", body):
        u = m.group(0).rstrip(".,);]")
        if any(x in u for x in ("generated", "assets.grok", "imagine", ".jpg", ".png", ".webp", "image")):
            add(u)
    for m in re.finditer(r'"(?:imageUrl|image_url|url)"\s*:\s*"([^"]+)"', body):
        add(m.group(1))
    for m in re.finditer(r'"generatedImageUrls"\s*:\s*\[(.*?)\]', body, re.S):
        for u in re.findall(r'"([^"]+)"', m.group(1)):
            add(u)
    for m in re.finditer(r'users/[^"\s]+/generated/[^"\s]+', body):
        add(m.group(0))
    return found


def absolute_asset_url(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("https://") or value.startswith("http://"):
        return value
    return "https://assets.grok.com/" + value.lstrip("/")


def download_asset(
    url: str,
    sso: str,
    *,
    cookie_override: str | None = None,
    user_agent: str = UA,
    dest_dir: Path | None = None,
) -> dict:
    """Download assets.grok.com image with SSO (browser direct open → 403)."""
    dest_dir = dest_dir or (TMP / "lite-images")
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = "part0.jpg" if "-part-0" in url else Path(urlparse(url).path).name or "image.jpg"
    if name == "image.jpg":
        # disambiguate final vs other
        stem = "final" if "/generated/" in url and "-part-" not in url else uuid.uuid4().hex[:8]
        name = f"{stem}.jpg"
    path = dest_dir / name
    headers = {
        "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "user-agent": user_agent,
        "referer": "https://grok.com/",
        "origin": "https://grok.com",
        "cookie": cookie_override or cookie_header(sso),
    }
    r = crequests.get(
        url,
        headers=headers,
        impersonate=IMPERSONATE,
        proxies=PROXIES,
        timeout=60,
    )
    path.write_bytes(r.content)
    return {
        "url": url,
        "file": str(path.resolve()),
        "name": name,
        "http": r.status_code,
        "content_type": r.headers.get("content-type"),
        "bytes": len(r.content),
        "jpeg": r.content[:3] == b"\xff\xd8\xff",
    }


def post_rest(
    sso: str,
    statsig: str | None,
    payload: dict,
    *,
    cookie_override: str | None = None,
    user_agent: str = UA,
) -> dict:
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
        "user-agent": user_agent,
        "cookie": cookie_override or cookie_header(sso),
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
    r = crequests.post(
        CHAT_URL,
        headers=headers,
        json=payload,
        impersonate=IMPERSONATE,
        proxies=PROXIES,
        timeout=120,
        stream=True,
    )
    chunks: list[bytes] = []
    total = 0
    limit = 2_000_000
    for chunk in r.iter_content(chunk_size=8192):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total >= limit:
            break
    body = b"".join(chunks).decode("utf-8", "replace")
    images = extract_image_urls(body)
    abs_images = [absolute_asset_url(u) for u in images]
    kind = classify_body(r.status_code, body, r.headers.get("cf-mitigated"))
    if images and r.status_code == 200:
        kind = "image_ok" if payload.get("enableImageGeneration") else kind
    return {
        "http": r.status_code,
        "cf": r.headers.get("cf-mitigated"),
        "elapsed_s": round(time.time() - t0, 2),
        "has_statsig": bool(statsig),
        "statsig_len": len(statsig) if statsig else 0,
        "kind": kind,
        "has_token_hint": ("token" in body.lower()) or ("PONG" in body),
        "image_count": len(images),
        "image_urls": abs_images[:6],
        "image_urls_prefix": [u[:120] for u in abs_images[:3]],
        "body_prefix": body[:320].replace("\n", " "),
    }


def playwright_sign(
    sso: str,
    path: str = CHAT_PATH,
    timeout_s: int = 90,
    *,
    channel: str | None = None,
    proxy: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """Obtain x-statsig-id via Playwright (Turbopack signer + UI capture fallback)."""
    from playwright.sync_api import sync_playwright

    result: dict = {"source": None, "statsigId": None, "error": None, "cookie": None, "notes": [], "candidates": []}
    token = sso.strip()
    if token.lower().startswith("sso="):
        token = token[4:].strip()
    proxy_server = (proxy or PROXY).strip()
    ua = user_agent or UA

    with sync_playwright() as p:
        launch_kwargs: dict = {
            "headless": os.environ.get("GROK_PW_HEADLESS", "0" if sys.platform == "win32" else "1") != "0",
            "proxy": {"server": proxy_server},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if channel:
            launch_kwargs["channel"] = channel
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(user_agent=ua, viewport={"width": 1400, "height": 900})
        context.add_init_script(TURBOPACK_HOOK)
        context.add_init_script(CAPTURE_HOOK)
        context.add_cookies(
            [
                {"name": "sso", "value": token, "domain": ".grok.com", "path": "/"},
                {"name": "sso-rw", "value": token, "domain": ".grok.com", "path": "/"},
            ]
        )
        page = context.new_page()
        net_caps: list[dict] = []

        def on_request(req):
            if "/rest/" not in req.url:
                return
            sig = req.headers.get("x-statsig-id") or ""
            if sig:
                net_caps.append({"path": urlparse(req.url).path, "method": req.method, "sig": sig})

        page.on("request", on_request)
        try:
            goto_err = ""
            for attempt in range(1, 4):
                try:
                    print(json.dumps({"event": "pw_goto", "attempt": attempt}, ensure_ascii=False), flush=True)
                    page.goto(
                        "https://grok.com/",
                        wait_until="domcontentloaded" if attempt < 3 else "commit",
                        timeout=min(45000, timeout_s * 1000),
                    )
                    goto_err = ""
                    break
                except Exception as exc:  # noqa: BLE001
                    goto_err = f"{type(exc).__name__}: {str(exc)[:160]}"
                    result["notes"].append(f"goto_retry:{attempt}:{goto_err[:80]}")
                    try:
                        page.wait_for_timeout(1500 * attempt)
                    except Exception:
                        pass
            if goto_err:
                raise RuntimeError(goto_err)
            page.wait_for_timeout(4000)
            print(json.dumps({"event": "pw_age_dismiss"}, ensure_ascii=False), flush=True)
            age_notes = _dismiss_age_gate(page)
            result["notes"].extend(age_notes)
            print(json.dumps({"event": "pw_age_notes", "notes": age_notes}, ensure_ascii=False), flush=True)
            # Wait for site verification meta used by Statsig signer.
            try:
                page.wait_for_function(
                    """() => !!document.querySelector('meta[name*=\"verification\"], meta[name*=\"grok-site\"]')""",
                    timeout=15000,
                )
                result["notes"].append("meta:ok")
            except Exception:
                result["notes"].append("meta:missing")
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"goto: {type(exc).__name__}: {str(exc)[:160]}"
            browser.close()
            return result

        # Wait for runtime + DOM, then try module IDs (incl. runtime cache keys)
        print(json.dumps({"event": "pw_turbopack_try"}, ensure_ascii=False), flush=True)
        deadline = time.time() + min(25, timeout_s)
        signed = None
        while time.time() < deadline and not (isinstance(signed, dict) and signed.get("statsigId")):
            try:
                signed = page.evaluate(
                    """async ({path, method, ids}) => {
                      if (!globalThis.__grokBridgeRuntime) return {wait: 'runtime'};
                      const bodyOk = document.body && document.body.childNodes && document.body.childNodes.length >= 3;
                      if (!bodyOk) return {wait: 'dom'};
                      await new Promise(r => setTimeout(r, 1200));
                      const runtime = globalThis.__grokBridgeRuntime;
                      const cache = runtime.c || runtime.m || runtime.modules || null;
                      const dyn = [];
                      if (cache && typeof cache === 'object') {
                        for (const k of Object.keys(cache).slice(0, 80)) {
                          const n = Number(k);
                          if (Number.isFinite(n) && n > 0) dyn.push(n);
                        }
                      }
                      const tryIds = [...new Set([...(ids||[]), ...dyn])].slice(0, 40);
                      const errs = [];
                      for (const id of tryIds) {
                        try {
                          const mod = await runtime.A(id);
                          if (!mod || typeof mod.default !== 'function') { errs.push(id+':no-default'); continue; }
                          const signer = mod.default();
                          const sig = String(await signer(path, method) || '');
                          if (sig && !sig.startsWith('x0:') && !sig.startsWith('eDA6') && sig.length > 20) {
                            return {statsigId: sig, moduleId: id};
                          }
                          errs.push(id+':empty');
                        } catch (e) {
                          errs.push(id+':'+String(e && e.message || e).slice(0,80));
                        }
                      }
                      return {wait: 'sign', errs: errs.slice(0, 10), tried: tryIds.length};
                    }""",
                    {"path": path, "method": "POST", "ids": list(SIGNER_MODULE_CANDIDATES)},
                )
            except Exception as exc:  # noqa: BLE001
                result["notes"].append(f"eval:{type(exc).__name__}")
                signed = {"wait": "eval_error"}
            if isinstance(signed, dict) and signed.get("statsigId"):
                break
            page.wait_for_timeout(900)

        if isinstance(signed, dict) and signed.get("statsigId"):
            result["statsigId"] = signed["statsigId"]
            result["source"] = f"turbopack:{signed.get('moduleId')}"
            result["notes"].append(f"module={signed.get('moduleId')}")
        else:
            result["notes"].append(f"turbopack_status={signed}")
            # UI drive fallback to capture live signatures on chat-capable paths
            try:
                print(json.dumps({"event": "pw_ui_drive"}, ensure_ascii=False), flush=True)
                result["notes"].extend(_dismiss_age_gate(page))
                page.wait_for_timeout(1500)
                filled = False
                for sel in ('textarea', '[contenteditable="true"]', 'div[role="textbox"]'):
                    loc = page.locator(sel).first
                    try:
                        if loc.count() == 0:
                            continue
                        loc.click(timeout=5000)
                        loc.fill("Reply with exactly: PONG")
                        page.keyboard.press("Enter")
                        result["notes"].append(f"ui:{sel}")
                        page.wait_for_timeout(8000)
                        filled = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        result["notes"].append(f"ui_try:{sel}:{type(exc).__name__}")
                if not filled:
                    page.keyboard.type("Reply with exactly: PONG", delay=20)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(8000)
                    result["notes"].append("ui:keyboard")
                # Prefer capturing the live conversations/new request while SPA signs it.
                try:
                    with page.expect_request(
                        lambda r: (
                            "/rest/app-chat/conversations/new" in r.url
                            and bool(r.headers.get("x-statsig-id"))
                        ),
                        timeout=15000,
                    ) as req_info:
                        # Re-press send if composer still has text
                        page.keyboard.press("Enter")
                    req = req_info.value
                    sig = req.headers.get("x-statsig-id") or ""
                    if sig:
                        result["statsigId"] = sig
                        result["source"] = "ui_expect:conversations/new"
                        result["notes"].append("expect_request:ok")
                except Exception as exc:  # noqa: BLE001
                    result["notes"].append(f"expect_request:{type(exc).__name__}")
            except Exception as exc:  # noqa: BLE001
                result["notes"].append(f"ui_err:{type(exc).__name__}:{str(exc)[:80]}")

            if result.get("statsigId"):
                pass
            else:
                js_caps = page.evaluate("() => (globalThis.__grokCapturedSigs || []).slice(-30)") or []
                for item in list(js_caps) + net_caps:
                    if item.get("path") == path and item.get("sig"):
                        result["statsigId"] = item["sig"]
                        result["source"] = "ui_capture"
                        break
                if not result["statsigId"]:
                    # Only chat/app-chat paths authorize conversations/new (products/rate-limits → anti-bot 403).
                    preferred = [
                        path,
                        CHAT_PATH,
                        "/rest/app-chat/conversations/new",
                    ]
                    for want in preferred:
                        for item in list(js_caps) + net_caps:
                            if item.get("path") == want and item.get("sig"):
                                result["statsigId"] = item["sig"]
                                result["source"] = f"ui_capture:{want}"
                                break
                        if result["statsigId"]:
                            break
                if not result["statsigId"]:
                    for item in list(js_caps) + net_caps:
                        pth = str(item.get("path", ""))
                        if (
                            item.get("sig")
                            and pth.startswith("/rest/app-chat/")
                            and "rate-limit" not in pth
                        ):
                            result["statsigId"] = item["sig"]
                            result["source"] = f"ui_capture_appchat:{pth}"
                            break

        cookies = context.cookies("https://grok.com/")
        result["cookie"] = "; ".join(f'{c["name"]}={c["value"]}' for c in cookies)
        result["cookie_names"] = sorted({c["name"] for c in cookies})
        try:
            result["statsigMeta"] = page.evaluate(
                """() => {
                  const pick = (sel) => {
                    const el = document.querySelector(sel);
                    return el ? (el.getAttribute('content') || '').trim() : '';
                  };
                  return pick('meta[name="twitter:site-verification"]')
                    || pick('meta[name="twitter:site―verification"]')
                    || pick('meta[name*="grok-site"]')
                    || pick('meta[name*="verification"]')
                    || null;
                }"""
            )
        except Exception:
            result["statsigMeta"] = None
        if not result.get("statsigMeta"):
            try:
                html = page.content()
                for pat in (
                    r'name="twitter:site-verification"\s+content="([^"]+)"',
                    r'name="twitter:site―verification"\s+content="([^"]+)"',
                ):
                    m = re.search(pat, html)
                    if m:
                        result["statsigMeta"] = m.group(1)
                        break
            except Exception:
                pass
        # All captured candidates for caller to probe (path-bound statsig).
        merged = []
        seen = set()
        js_caps = page.evaluate("() => (globalThis.__grokCapturedSigs || []).slice(-40)") or []
        for item in list(js_caps) + net_caps:
            sig = item.get("sig") or ""
            pth = str(item.get("path") or "")
            if not sig or not pth.startswith("/rest/"):
                continue
            key = (pth, sig[:32])
            if key in seen:
                continue
            seen.add(key)
            merged.append({"path": pth, "sig": sig, "src": item.get("src") or "cap"})
        if result.get("statsigId"):
            merged.insert(0, {"path": path, "sig": result["statsigId"], "src": result.get("source") or "primary"})
        result["candidates"] = merged[:20]
        if not result["statsigId"] and not result["error"]:
            result["error"] = "no statsig captured"
        try:
            TMP.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(TMP / "http-chat-image-sign.png"), full_page=False)
        except Exception:
            pass
        browser.close()
    return result


def panda_bridge_sign(sso: str, account_id: int) -> dict:
    """Fallback: reuse web_sso_http_canary bridge_sign helpers."""
    sys.path.insert(0, str(ROOT / "tools"))
    import web_sso_http_canary as canary  # noqa: WPS433

    used_bridge = False
    try:
        pf = canary.panda_preflight()
        if not pf.get("ok"):
            return {"http": 0, "error": f"preflight:{pf}", "body": {}}
        canary.sync_bridge_app()
        started = canary.start_bridge()
        used_bridge = True
        if not started.get("ok"):
            return {"http": 0, "error": f"start_bridge:{started}", "body": {}}
        egress = canary.export_web_egress()
        ua = egress.get("userAgent") or UA
        sign_res = canary.bridge_sign(
            sso,
            account_id,
            proxy_url=egress["proxyUrl"],
            user_agent=ua,
            timeout_ms=120000,
        )
        sign_res["_egress"] = {
            "id": egress.get("id"),
            "name": egress.get("name"),
            "source": egress.get("source"),
            "proxy_host": egress.get("proxy_host"),
        }
        sign_res["_ua"] = ua
        return sign_res
    finally:
        if used_bridge:
            try:
                canary.stop_bridge()
            except Exception:
                pass


def load_accounts(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [a for a in data.get("accounts", []) if a.get("sso")]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sso-file", type=Path, default=TMP / "web-sso-canary.json")
    parser.add_argument("--account-id", type=int)
    parser.add_argument("--skip-panda-fallback", action="store_true")
    parser.add_argument("--sign-only-local", action="store_true", help="never call panda bridge")
    parser.add_argument(
        "--image-prompt",
        default="a simple red apple on white background, minimal",
        help="Lite Drawing prompt body (without 'Drawing:' prefix)",
    )
    args = parser.parse_args()
    image_message = "Drawing: " + str(args.image_prompt).strip()
    image_dir = TMP / "lite-images" / time.strftime("%Y%m%d-%H%M%S")

    TMP.mkdir(parents=True, exist_ok=True)
    if not args.sso_file.exists():
        # export via existing canary helper
        sys.path.insert(0, str(ROOT / "tools"))
        import web_sso_http_canary as canary  # noqa: WPS433

        accounts = canary.export_sso(2)
        args.sso_file.write_text(json.dumps({"count": len(accounts), "accounts": accounts}, ensure_ascii=False), encoding="utf-8")

    accounts = load_accounts(args.sso_file)
    if args.account_id:
        accounts = [a for a in accounts if a["id"] == args.account_id]
    if not accounts:
        raise SystemExit("no accounts")
    acc = accounts[0]
    aid = acc["id"]
    sso = acc["sso"]
    print(json.dumps({"event": "start", "account_id": aid, "proxy": PROXY}))

    row: dict = {"account_id": aid}

    # A: no signature
    row["A_no_sig"] = post_rest(sso, None, chat_payload("Reply with exactly: PONG"))
    print(json.dumps({"event": "A_no_sig", "account_id": aid, "http": row["A_no_sig"]["http"], "kind": row["A_no_sig"]["kind"], "elapsed_s": row["A_no_sig"]["elapsed_s"]}))

    # B: sign + probe candidates until chat_ok (statsig is path-sensitive).
    sign_local = playwright_sign(sso, CHAT_PATH)
    cookie_override = sign_local.get("cookie")
    chat_ua = UA
    candidates = list(sign_local.get("candidates") or [])
    if sign_local.get("statsigId"):
        candidates.insert(
            0,
            {
                "path": CHAT_PATH,
                "sig": sign_local["statsigId"],
                "src": sign_local.get("source") or "primary",
            },
        )
    # Prefer exact conversations/new, then other app-chat, skip products/rate-limits.
    def cand_rank(c: dict) -> tuple:
        p = str(c.get("path") or "")
        if p == CHAT_PATH or p.endswith("/conversations/new"):
            return (0, p)
        if p.startswith("/rest/app-chat/"):
            return (1, p)
        if "product" in p or "rate-limit" in p or "media" in p:
            return (9, p)
        return (5, p)

    candidates = sorted(candidates, key=cand_rank)
    # dedupe by sig
    seen_sig = set()
    uniq = []
    for c in candidates:
        s = c.get("sig") or ""
        if not s or s in seen_sig:
            continue
        seen_sig.add(s)
        uniq.append(c)
    candidates = uniq[:12]

    statsig = None
    row["B_sign"] = {
        "source": sign_local.get("source"),
        "has_statsig": bool(sign_local.get("statsigId")),
        "statsig_len": len(sign_local.get("statsigId") or ""),
        "error": sign_local.get("error"),
        "notes": sign_local.get("notes", [])[:12],
        "cookie_names": sign_local.get("cookie_names"),
        "candidate_paths": [c.get("path") for c in candidates],
    }
    print(
        json.dumps(
            {
                "event": "B_sign_local",
                "account_id": aid,
                "source": row["B_sign"]["source"],
                "candidates": row["B_sign"]["candidate_paths"],
                "error": row["B_sign"].get("error"),
            },
            ensure_ascii=False,
        )
    )

    probes = []
    for c in candidates:
        probe = post_rest(
            sso,
            c["sig"],
            chat_payload("Reply with exactly: PONG"),
            cookie_override=cookie_override,
            user_agent=chat_ua,
        )
        probes.append({"path": c.get("path"), "src": c.get("src"), "kind": probe.get("kind"), "http": probe.get("http")})
        print(
            json.dumps(
                {
                    "event": "B_probe",
                    "account_id": aid,
                    "path": c.get("path"),
                    "src": c.get("src"),
                    "http": probe.get("http"),
                    "kind": probe.get("kind"),
                },
                ensure_ascii=False,
            )
        )
        if probe.get("kind") == "chat_ok":
            statsig = c["sig"]
            row["B_sign"]["source"] = f"probed:{c.get('path')}"
            row["B_sign"]["statsig_len"] = len(statsig)
            row["C_signed_chat"] = probe
            break
    row["B_probes"] = probes

    if not statsig and not args.skip_panda_fallback and not args.sign_only_local:
        print(json.dumps({"event": "B_sign_panda_fallback", "account_id": aid}))
        panda = panda_bridge_sign(sso, aid)
        body = panda.get("body") if isinstance(panda.get("body"), dict) else {}
        statsig = body.get("statsigId")
        if panda.get("_ua"):
            chat_ua = panda["_ua"]
        row["B_sign_panda"] = {
            "http": panda.get("http"),
            "has_statsig": bool(statsig),
            "statsig_len": len(statsig) if statsig else 0,
            "error": panda.get("error") or body.get("error"),
            "egress": panda.get("_egress"),
        }
        print(
            json.dumps(
                {
                    "event": "B_sign_panda",
                    "account_id": aid,
                    "http": row["B_sign_panda"]["http"],
                    "has_statsig": row["B_sign_panda"]["has_statsig"],
                    "statsig_len": row["B_sign_panda"]["statsig_len"],
                    "error": row["B_sign_panda"].get("error"),
                },
                ensure_ascii=False,
            )
        )
        cookie_override = None

    # C: signed chat (if not already set by probe)
    if "C_signed_chat" not in row:
        if statsig:
            row["C_signed_chat"] = post_rest(
                sso,
                statsig,
                chat_payload("Reply with exactly: PONG"),
                cookie_override=cookie_override,
                user_agent=chat_ua,
            )
        else:
            row["C_signed_chat"] = {"skipped": True, "reason": "no working statsig"}
    print(
        json.dumps(
            {
                "event": "C_signed_chat",
                "account_id": aid,
                "http": row["C_signed_chat"].get("http"),
                "kind": row["C_signed_chat"].get("kind") or row["C_signed_chat"].get("reason"),
                "has_token_hint": row["C_signed_chat"].get("has_token_hint"),
                "elapsed_s": row["C_signed_chat"].get("elapsed_s"),
            },
            ensure_ascii=False,
        )
    )

    # D: Lite image — always refresh ticket (statsig is short-lived / path-sensitive)
    image_sig = None
    image_dir.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"event": "D_resign_start", "account_id": aid, "prompt": image_message}, ensure_ascii=False))
    again = playwright_sign(sso, CHAT_PATH, timeout_s=90)
    row["D_resign"] = {
        "source": again.get("source"),
        "has_statsig": bool(again.get("statsigId")),
        "statsig_len": len(again.get("statsigId") or ""),
        "error": again.get("error"),
        "notes": (again.get("notes") or [])[:8],
    }
    if again.get("statsigId"):
        image_sig = again["statsigId"]
    if again.get("cookie"):
        cookie_override = again["cookie"]
    # Prefer a candidate that probes OK for chat path, else first sig
    for c in again.get("candidates") or []:
        if not c.get("sig"):
            continue
        probe = post_rest(
            sso,
            c["sig"],
            chat_payload("Reply with exactly: PONG"),
            cookie_override=cookie_override,
            user_agent=chat_ua,
        )
        if probe.get("kind") == "chat_ok":
            image_sig = c["sig"]
            row["D_resign"]["source"] = f"probed:{c.get('path')}"
            row["D_resign"]["statsig_len"] = len(image_sig)
            break
    if not image_sig:
        image_sig = statsig  # last resort: reuse C ticket

    if image_sig:
        row["D_prompt"] = image_message
        row["D_lite_image"] = post_rest(
            sso,
            image_sig,
            chat_payload(image_message, enable_image=True),
            cookie_override=cookie_override,
            user_agent=chat_ua,
        )
        # one retry on anti-bot with another fresh sign
        if (row["D_lite_image"].get("kind") == "anti_bot_403") or (row["D_lite_image"].get("http") == 403):
            print(json.dumps({"event": "D_retry_sign", "account_id": aid}, ensure_ascii=False))
            retry = playwright_sign(sso, CHAT_PATH, timeout_s=90)
            retry_sig = retry.get("statsigId")
            for c in retry.get("candidates") or []:
                if not c.get("sig"):
                    continue
                probe = post_rest(
                    sso,
                    c["sig"],
                    chat_payload("Reply with exactly: PONG"),
                    cookie_override=retry.get("cookie") or cookie_override,
                    user_agent=chat_ua,
                )
                if probe.get("kind") == "chat_ok":
                    retry_sig = c["sig"]
                    if retry.get("cookie"):
                        cookie_override = retry["cookie"]
                    break
            if retry_sig:
                row["D_lite_image"] = post_rest(
                    sso,
                    retry_sig,
                    chat_payload(image_message, enable_image=True),
                    cookie_override=cookie_override,
                    user_agent=chat_ua,
                )
                row["D_resign_retry"] = {"source": retry.get("source"), "statsig_len": len(retry_sig)}
    else:
        row["D_lite_image"] = {"skipped": True, "reason": "no statsig"}
    print(
        json.dumps(
            {
                "event": "D_lite_image",
                "account_id": aid,
                "http": row["D_lite_image"].get("http"),
                "kind": row["D_lite_image"].get("kind") or row["D_lite_image"].get("reason"),
                "image_count": row["D_lite_image"].get("image_count"),
                "image_urls": row["D_lite_image"].get("image_urls"),
                "image_urls_prefix": row["D_lite_image"].get("image_urls_prefix"),
                "elapsed_s": row["D_lite_image"].get("elapsed_s"),
            },
            ensure_ascii=False,
        )
    )

    # Persist Lite images locally (assets.grok.com needs SSO; bare browser open → 403)
    local_files: list[dict] = []
    for url in (row.get("D_lite_image") or {}).get("image_urls") or []:
        try:
            saved = download_asset(
                absolute_asset_url(url),
                sso,
                cookie_override=cookie_override,
                user_agent=chat_ua,
                dest_dir=image_dir,
            )
            local_files.append(saved)
            print(json.dumps({"event": "D_download", **{k: saved[k] for k in ("http", "bytes", "file", "name")}}, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            local_files.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
            print(json.dumps({"event": "D_download", "error": str(exc)[:160]}, ensure_ascii=False))
    if local_files:
        row["D_local_files"] = local_files

    out = TMP / "web-http-chat-image-canary-result.json"
    out.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    a_ok = (row.get("A_no_sig") or {}).get("kind") == "anti_bot_403"
    c_ok = (row.get("C_signed_chat") or {}).get("kind") == "chat_ok"
    d_ok = (row.get("D_lite_image") or {}).get("kind") == "image_ok" or ((row.get("D_lite_image") or {}).get("image_count") or 0) > 0
    if a_ok and c_ok and d_ok:
        baseline = {
            "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "chain": "docs/http-reverse-lite-chain.md",
            "script": "tools/web_http_chat_image_canary.v1.py",
            "account_id": aid,
            "acceptance": {"A_anti_bot": a_ok, "C_chat_ok": c_ok, "D_image_ok": d_ok},
            "C_signed_chat": {
                "http": (row.get("C_signed_chat") or {}).get("http"),
                "kind": (row.get("C_signed_chat") or {}).get("kind"),
                "elapsed_s": (row.get("C_signed_chat") or {}).get("elapsed_s"),
            },
            "D_lite_image": {
                "http": (row.get("D_lite_image") or {}).get("http"),
                "kind": (row.get("D_lite_image") or {}).get("kind"),
                "elapsed_s": (row.get("D_lite_image") or {}).get("elapsed_s"),
                "image_count": (row.get("D_lite_image") or {}).get("image_count"),
                "image_urls": (row.get("D_lite_image") or {}).get("image_urls") or [],
                "local_files": [
                    {"name": x.get("name"), "file": x.get("file"), "bytes": x.get("bytes"), "http": x.get("http")}
                    for x in (row.get("D_local_files") or [])
                    if x.get("file")
                ],
            },
            "B_sign": {
                "source": (row.get("B_sign") or {}).get("source"),
                "statsig_len": (row.get("B_sign") or {}).get("statsig_len"),
            },
        }
        (TMP / "web-http-chat-image-canary-baseline.json").write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "event": "acceptance",
                "A_anti_bot": a_ok,
                "C_chat_ok": c_ok,
                "D_image_ok": d_ok,
                "path": str(out),
                "baseline": str(TMP / "web-http-chat-image-canary-baseline.json") if (a_ok and c_ok and d_ok) else None,
                "image_urls": (row.get("D_lite_image") or {}).get("image_urls") or [],
            },
            ensure_ascii=False,
        )
    )
    if not (a_ok and c_ok and d_ok):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
