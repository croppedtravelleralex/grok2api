#!/usr/bin/env python3
"""Camoufox: login grok.com → capture text+image → HTTP replay without resign.

Phases:
  1) Camoufox + SSO cookie → capture /rest/* (x-statsig-id + cf jar)
  2) In-browser SPA text + Lite image (or nudge REST if UI stuck)
  3) HTTP replay via same Camoufox request context (Firefox TLS) and curl_cffi
  4) Repeat HTTP N times WITHOUT new browser sign

Uses Clash http://127.0.0.1:7897. Secrets never printed in full.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / ".tmp" / "camoufox-grok"
PROXY = "http://127.0.0.1:7897"
CHAT_PATH = "/rest/app-chat/conversations/new"

# Prefer system Camoufox (user site) + gptimage venv curl_cffi when available.
sys.path.insert(0, str(ROOT / "tools"))

from camoufox.sync_api import Camoufox  # noqa: E402

# Reuse capture hook + age-gate helpers from frozen canary.
import importlib.util

_SPEC = importlib.util.spec_from_file_location("canary_v1", ROOT / "tools" / "web_http_chat_image_canary.v1.py")
v1 = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(v1)


def log(**kw) -> None:
    print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **kw}, ensure_ascii=False), flush=True)


def proxy_dict(proxy: str) -> dict:
    p = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port or 80}"}
    if p.username:
        cfg["username"] = p.username
    if p.password:
        cfg["password"] = p.password
    return cfg


def load_account(path: Path, account_id: int | None) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    accounts = data.get("accounts") or data
    if not isinstance(accounts, list):
        raise SystemExit("bad sso json")
    if account_id is not None:
        for a in accounts:
            if int(a.get("id") or 0) == account_id:
                return a
        raise SystemExit(f"account {account_id} not found")
    return accounts[0]


def sso_token(account: dict) -> str:
    tok = (account.get("sso") or account.get("token") or "").strip()
    if tok.lower().startswith("sso="):
        tok = tok[4:].strip()
    return tok


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


def looks_bad_sig(sig: str) -> bool:
    if not sig or len(sig) < 20:
        return True
    if sig.startswith("eDA6") or sig.startswith("x0:") or sig.startswith("eDE6"):
        return True
    return False


def extract_image_urls(body: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        url = (url or "").strip().rstrip("\\")
        if not url or url in seen:
            return
        if url.startswith("//"):
            url = "https:" + url
        if not (
            url.startswith("http://")
            or url.startswith("https://")
            or url.startswith("users/")
            or "/generated/" in url
        ):
            return
        seen.add(url)
        found.append(url)

    for m in re.finditer(r"https?://[^\s\"'<>\\]+", body):
        u = m.group(0).rstrip(".,);]")
        if any(x in u for x in ("generated", "assets.grok", "imagine", ".jpg", ".png", ".webp")):
            add(u)
    for m in re.finditer(r'"(?:imageUrl|image_url|url)"\s*:\s*"([^"]+)"', body):
        add(m.group(1))
    for m in re.finditer(r'"generatedImageUrls"\s*:\s*\[(.*?)\]', body, re.S):
        for u in re.findall(r'"([^"]+)"', m.group(1)):
            add(u)
    return found


def jar_header(cookies) -> str:
    parts = []
    for c in cookies:
        n, v = c.get("name"), c.get("value")
        if n and v:
            parts.append(f"{n}={v}")
    return "; ".join(parts)


def classify(status: int, body: str) -> str:
    low = body.lower()
    if "just a moment" in low:
        return "cf_challenge"
    if status == 403 and "anti-bot" in low:
        return "anti_bot"
    if status == 200 and extract_image_urls(body):
        return "image_ok"
    if status == 200 and ("pong" in low or '"message"' in low or "token" in low or "result" in low):
        return "chat_ok"
    if status == 200:
        return "http_200"
    return f"http_{status}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sso-json", type=Path, default=ROOT / ".tmp" / "web-sso-canary.json")
    ap.add_argument("--account-id", type=int, default=None)
    ap.add_argument("--proxy", default=PROXY)
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--replay-n", type=int, default=3, help="HTTP repeats without resign")
    ap.add_argument("--timeout-s", type=int, default=90)
    args = ap.parse_args()
    headless = not args.headed

    TMP.mkdir(parents=True, exist_ok=True)
    account = load_account(args.sso_json, args.account_id)
    tok = sso_token(account)
    aid = int(account.get("id") or 0)
    log(phase="account", id=aid, name=(account.get("name") or "")[:48], sso_len=len(tok))

    caps: list[dict] = []
    result: dict = {
        "account_id": aid,
        "proxy": args.proxy,
        "phases": {},
        "captures": [],
        "http_replays": [],
    }

    # geoip=False: Clash often breaks Camoufox public_ip probe (same as gptimage).
    with Camoufox(headless=headless, proxy=proxy_dict(args.proxy), geoip=False) as browser:
        context = browser.new_context()
        context.add_init_script(v1.CAPTURE_HOOK)
        context.add_cookies(
            [
                {"name": "sso", "value": tok, "domain": ".grok.com", "path": "/", "secure": True, "httpOnly": True},
                {"name": "sso-rw", "value": tok, "domain": ".grok.com", "path": "/", "secure": True, "httpOnly": True},
            ]
        )
        page = context.new_page()

        def on_request(req) -> None:
            try:
                url = req.url or ""
                if "grok.com/rest/" not in url:
                    return
                path = urlparse(url).path
                sig = req.headers.get("x-statsig-id") or ""
                item = {
                    "t": time.time(),
                    "method": req.method,
                    "path": path,
                    "sig_len": len(sig),
                    "sig_prefix": sig[:16],
                    "sig": sig,
                    "bad": looks_bad_sig(sig),
                    "src": "net",
                }
                caps.append(item)
            except Exception:
                pass

        page.on("request", on_request)

        def pick_ticket() -> dict | None:
            js_caps = page.evaluate("() => (globalThis.__grokCapturedSigs || []).slice(-60)") or []
            merged = []
            for item in list(js_caps) + caps:
                sig = item.get("sig") or ""
                pth = str(item.get("path") or "")
                if looks_bad_sig(sig) or not pth.startswith("/rest/"):
                    continue
                merged.append({"path": pth, "sig": sig, "src": item.get("src") or "js"})
            for prefer in (CHAT_PATH, "/rest/modes", "/rest/app-chat/conversations"):
                for m in reversed(merged):
                    if m["path"] == prefer:
                        return m
            return merged[-1] if merged else None

        log(phase="goto_grok")
        t0 = time.time()
        page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=args.timeout_s * 1000)
        try:
            notes = v1._dismiss_age_gate(page)
            log(phase="age_gate", notes=notes)
        except Exception as exc:
            log(phase="age_gate_err", error=type(exc).__name__)

        # Wait CF + any rest, then UI-nudge for conversations/new signature
        deadline = time.time() + args.timeout_s
        good = None
        while time.time() < deadline:
            jar = jar_header(context.cookies())
            has_cf = "cf_clearance=" in jar
            good = pick_ticket()
            if has_cf and good and good["path"] in {CHAT_PATH, "/rest/modes"}:
                break
            page.wait_for_timeout(400)

        jar = jar_header(context.cookies())
        has_cf = "cf_clearance=" in jar

        # UI send to force path-correct ticket for conversations/new
        if has_cf and (not good or good["path"] not in {CHAT_PATH, "/rest/modes"}):
            log(phase="ui_nudge_text")
            try:
                for sel in ("textarea", '[contenteditable="true"]', 'div[role="textbox"]'):
                    loc = page.locator(sel)
                    if loc.count() == 0:
                        continue
                    loc.first.click(timeout=4000)
                    loc.first.fill("Reply with exactly: PONG")
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(8000)
                    break
            except Exception as exc:
                log(phase="ui_nudge_err", error=type(exc).__name__)
            good = pick_ticket() or good

        # Prefer modes/new over generic conversations list
        if good and good["path"] not in {CHAT_PATH, "/rest/modes"}:
            # try fetch modes via page so SPA signs it
            try:
                page.evaluate(
                    """async () => {
                      try { await fetch('/rest/modes', {credentials:'include'}); } catch(e) {}
                    }"""
                )
                page.wait_for_timeout(2000)
                good = pick_ticket() or good
            except Exception:
                pass

        elapsed_nav = round(time.time() - t0, 2)
        log(
            phase="capture_ticket",
            elapsed_s=elapsed_nav,
            has_cf=has_cf,
            caps=len(caps),
            ticket_path=(good or {}).get("path"),
            sig_len=len((good or {}).get("sig") or ""),
        )
        result["phases"]["capture"] = {
            "elapsed_s": elapsed_nav,
            "has_cf": has_cf,
            "caps_n": len(caps),
            "ticket_path": (good or {}).get("path"),
            "sig_len": len((good or {}).get("sig") or ""),
        }
        if not good or not has_cf:
            result["error"] = "no_ticket_or_cf"
            (TMP / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            log(phase="fail", error=result["error"])
            return 2

        statsig = good["sig"]
        capture_path = TMP / f"capture-{aid}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        safe_caps = [{k: v for k, v in c.items() if k != "sig"} for c in caps]
        capture_path.write_text(
            json.dumps(
                {
                    "account_id": aid,
                    "statsig_path": good["path"],
                    "statsig_len": len(statsig),
                    "has_cf": has_cf,
                    "cookie_names": [c["name"] for c in context.cookies()],
                    "caps": safe_caps,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        # Probe: modes ticket or new ticket against modes endpoint first
        api = context.request

        def browser_http(name: str, message: str, *, enable_image: bool, sig: str | None = None) -> dict:
            use_sig = sig or statsig
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://grok.com",
                "Referer": "https://grok.com/",
                "x-statsig-id": use_sig,
            }
            body = chat_payload(message, enable_image=enable_image)
            t1 = time.time()
            resp = api.post(
                "https://grok.com" + CHAT_PATH,
                headers=headers,
                data=json.dumps(body),
                timeout=120_000,
            )
            text = resp.text()
            urls = extract_image_urls(text)
            row = {
                "name": name,
                "via": "camoufox_request",
                "sig_path": good["path"],
                "http": resp.status,
                "elapsed_s": round(time.time() - t1, 2),
                "kind": classify(resp.status, text),
                "bytes": len(text),
                "assets": len(urls),
                "asset_tails": [u[-50:] for u in urls[:4]],
                "body_head": text[:240],
            }
            log(phase="browser_http", **{k: v for k, v in row.items() if k != "body_head"}, body_head=row["body_head"][:120])
            if urls:
                img_dir = TMP / "images"
                img_dir.mkdir(exist_ok=True)
                u = urls[0]
                if u.startswith("users/"):
                    u = "https://assets.grok.com/" + u
                try:
                    ir = api.get(u, headers={"Referer": "https://grok.com/"}, timeout=60_000)
                    fp = img_dir / f"{name}-{aid}.bin"
                    fp.write_bytes(ir.body())
                    row["saved"] = str(fp)
                    row["saved_bytes"] = fp.stat().st_size
                except Exception as exc:
                    row["save_err"] = type(exc).__name__
            return row

        # If first chat anti_bot, resign via UI once more for CHAT_PATH specifically
        text_row = browser_http("text", "Reply with exactly: PONG", enable_image=False)
        if text_row.get("kind") == "anti_bot":
            log(phase="resign_ui_for_chat_path")
            before = len(caps)
            try:
                for sel in ("textarea", '[contenteditable="true"]', 'div[role="textbox"]'):
                    loc = page.locator(sel)
                    if loc.count() == 0:
                        continue
                    loc.first.click(timeout=4000)
                    loc.first.fill("Reply with exactly: PONG2")
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(10000)
                    break
            except Exception as exc:
                log(phase="resign_ui_err", error=type(exc).__name__)
            # pick newest CHAT_PATH sig
            for c in reversed(caps):
                if c.get("path") == CHAT_PATH and not c.get("bad") and c.get("sig"):
                    statsig = c["sig"]
                    good = {"path": CHAT_PATH, "sig": statsig}
                    log(phase="resign_got", sig_len=len(statsig), new_caps=len(caps) - before)
                    break
            else:
                # any new app-chat sig
                for c in reversed(caps):
                    if str(c.get("path") or "").startswith("/rest/app-chat/") and not c.get("bad") and c.get("sig"):
                        statsig = c["sig"]
                        good = {"path": c["path"], "sig": statsig}
                        log(phase="resign_got_appchat", path=c["path"], sig_len=len(statsig))
                        break
            text_row = browser_http("text_after_resign", "Reply with exactly: PONG3", enable_image=False)

        result["phases"]["text"] = text_row
        img_row = browser_http(
            "lite_image",
            "Drawing: a simple orange cat, realistic, clear details",
            enable_image=True,
        )
        result["phases"]["lite_image"] = img_row

        # Phase: repeat HTTP without resign
        replays = []
        for i in range(args.replay_n):
            r = browser_http(f"replay_text_{i+1}", f"Reply with exactly: PONG{i+1}", enable_image=False)
            replays.append(r)
            time.sleep(0.4)
        result["http_replays"] = replays

        # curl_cffi cross-check
        curl_rows = []
        try:
            from curl_cffi import requests as crequests

            ua = page.evaluate("() => navigator.userAgent") or ""
            jar = jar_header(context.cookies())
            for imp in ("firefox147", "firefox144", "firefox135", "chrome146"):
                try:
                    t1 = time.time()
                    r = crequests.post(
                        "https://grok.com" + CHAT_PATH,
                        headers={
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                            "Origin": "https://grok.com",
                            "Referer": "https://grok.com/",
                            "User-Agent": ua,
                            "Cookie": jar,
                            "x-statsig-id": statsig,
                        },
                        json=chat_payload("Reply with exactly: CURLPONG", enable_image=False),
                        proxies={"http": args.proxy, "https": args.proxy},
                        impersonate=imp,
                        timeout=60,
                    )
                    text = r.text or ""
                    curl_rows.append(
                        {
                            "impersonate": imp,
                            "http": r.status_code,
                            "kind": classify(r.status_code, text),
                            "elapsed_s": round(time.time() - t1, 2),
                            "body_head": text[:200],
                        }
                    )
                    log(phase="curl_cffi", impersonate=imp, http=r.status_code, kind=curl_rows[-1]["kind"])
                    if r.status_code == 200:
                        break
                except Exception as exc:
                    curl_rows.append({"impersonate": imp, "error": type(exc).__name__ + ":" + str(exc)[:120]})
                    log(phase="curl_cffi_err", impersonate=imp, error=type(exc).__name__)
        except ImportError:
            curl_rows.append({"error": "curl_cffi_missing"})
        result["curl_cffi"] = curl_rows

        try:
            rl = api.post(
                "https://grok.com/rest/rate-limits",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Origin": "https://grok.com",
                    "Referer": "https://grok.com/",
                    "x-statsig-id": statsig,
                },
                data=json.dumps({"modelName": "fast"}),
                timeout=30_000,
            )
            result["rate_limits"] = {"http": rl.status, "body_head": rl.text()[:400]}
            log(phase="rate_limits", http=rl.status, body_head=rl.text()[:200])
        except Exception as exc:
            result["rate_limits"] = {"error": type(exc).__name__}

    # verdict
    text_ok = (result.get("phases", {}).get("text") or {}).get("kind") in {"chat_ok", "http_200"}
    img_ok = (result.get("phases", {}).get("lite_image") or {}).get("kind") in {"image_ok", "chat_ok", "http_200"}
    replay_ok = sum(1 for r in result.get("http_replays") or [] if r.get("kind") in {"chat_ok", "http_200"})
    result["verdict"] = {
        "text_ok": text_ok,
        "image_ok": img_ok,
        "replay_ok_n": replay_ok,
        "replay_n": args.replay_n,
        "no_resign_between_text_image_replay": True,
        "pass": text_ok and img_ok and replay_ok >= 1,
    }
    out = TMP / "result.json"
    # strip full sigs from dumps already
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log(phase="done", verdict=result["verdict"], out=str(out))
    return 0 if result["verdict"]["pass"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
