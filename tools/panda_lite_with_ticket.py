#!/usr/bin/env python3
"""Consume a local Chrome ticket bundle and run Lite image HTTP on Panda.

Ticket JSON (stdin or TICKET_FILE):
  account_id, statsig?, statsig_meta?, cookie, user_agent?, prompt?, signed_at?

statsig_meta (twitter:site-verification) lets Panda refresh path-correct statsig at use time.
SSO is resolved from Panda backend.db by account_id.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:
    from curl_cffi import requests as crequests
except ImportError:
    print("curl_cffi required", file=sys.stderr)
    raise SystemExit(2)

DB = Path("/opt/grok2api/data/backend.db")
CONFIG = Path("/opt/grok2api/config.yaml")
CHAT_PATH = "/rest/app-chat/conversations/new"
CHAT_URL = "https://grok.com" + CHAT_PATH
GROK_BASE = "https://grok.com"
SIGNER_URLS = tuple(
    u.strip()
    for u in os.environ.get(
        "GROK_SIGNER_URLS",
        "http://172.22.0.2:8788/sign,http://grok-signer:8788/sign",
    ).split(",")
    if u.strip()
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
OUT = Path(os.environ.get("TICKET_OUT", "/tmp/panda-lite-ticket-out"))
MAX_LITE_ATTEMPTS = int(os.environ.get("PANDA_LITE_ATTEMPTS", "4"))


def log(event: str, **kw) -> None:
    row = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **kw}
    print(json.dumps(row, ensure_ascii=False), flush=True)


def load_key() -> bytes:
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        if "credentialEncryptionKey" in line:
            return base64.b64decode(line.split(":", 1)[1].strip().strip("\"'"))
    raise SystemExit("credentialEncryptionKey missing")


def decrypt_sso(account_id: int) -> str:
    import sqlite3

    aes = AESGCM(load_key())
    row = sqlite3.connect(DB).execute(
        "SELECT encrypted_primary FROM account_credentials WHERE account_id=?",
        (account_id,),
    ).fetchone()
    if not row:
        raise SystemExit(f"no credentials for account {account_id}")
    blob = row[0].strip()
    pad = "=" * ((4 - len(blob) % 4) % 4)
    raw = base64.b64decode(blob + pad)
    token = aes.decrypt(raw[:12], raw[12:], None).decode().strip()
    if token.lower().startswith("sso="):
        token = token[4:].strip()
    return token


def load_egress(scope: str) -> tuple[str, str, str]:
    import sqlite3

    aes = AESGCM(load_key())
    row = sqlite3.connect(DB).execute(
        """
        SELECT encrypted_proxy_url, user_agent, encrypted_cloudflare_cookie
        FROM egress_nodes WHERE scope=? AND enabled=1
        ORDER BY health DESC, id ASC LIMIT 1
        """,
        (scope,),
    ).fetchone()
    if not row:
        raise SystemExit(f"no enabled egress for {scope}")

    def decrypt_blob(blob: str) -> str:
        blob = (blob or "").strip()
        if not blob:
            return ""
        pad = "=" * ((4 - len(blob) % 4) % 4)
        raw = base64.b64decode(blob + pad)
        return aes.decrypt(raw[:12], raw[12:], None).decode()

    proxy = decrypt_blob(row[0])
    ua = (row[1] or "").strip() or UA
    cf = sanitize_cf_cookies(decrypt_blob(row[2]))
    return proxy, ua, cf


def sanitize_cf_cookies(raw: str) -> str:
    keep: list[str] = []
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name = part.split("=", 1)[0].strip().lower()
        if name in ("cf_clearance", "__cf_bm", "_cfuvid") or name.startswith("cf_chl"):
            keep.append(part)
    return "; ".join(keep)


def sanitize_ticket_cookie(sso: str, cookie_extra: str, egress_cf: str = "") -> str:
    parts = [f"sso={sso}", f"sso-rw={sso}"]
    cf = sanitize_cf_cookies(egress_cf)
    if cf:
        parts.append(cf)
    extra = (cookie_extra or "").strip().strip(";")
    if extra:
        keep: list[str] = []
        for part in extra.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name = part.split("=", 1)[0].strip().lower()
            if name in ("sso", "sso-rw", "cf_clearance", "__cf_bm", "_cfuvid") or name.startswith("cf_chl"):
                continue
            keep.append(part)
        if keep:
            parts.append("; ".join(keep))
    return "; ".join(parts)


def merge_cookie(sso: str, cookie_extra: str, egress_cf: str) -> str:
    cookie = sanitize_ticket_cookie(sso, cookie_extra, egress_cf)
    if "cf_clearance=" in cookie:
        return cookie
    for part in (cookie_extra or "").split(";"):
        part = part.strip()
        if not part:
            continue
        name = part.split("=", 1)[0].strip().lower()
        if name in ("cf_clearance", "__cf_bm", "_cfuvid") or name.startswith("cf_chl"):
            return f"{cookie}; {part}"
    return cookie


def warm_egress_cookie(proxy_url: str, user_agent: str, cookie: str) -> tuple[str, dict]:
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "user-agent": user_agent,
        "cookie": cookie,
    }
    proxies = {"http": proxy_url, "https": proxy_url}
    t0 = time.time()
    sess = crequests.Session()
    r = sess.get(
        GROK_BASE + "/",
        headers=headers,
        proxies=proxies,
        impersonate="chrome146",
        timeout=60,
        allow_redirects=True,
    )
    jar_bits: list[str] = []
    for name, value in sess.cookies.items():
        low = name.lower()
        if low in ("cf_clearance", "__cf_bm", "_cfuvid") or low.startswith("cf_chl"):
            jar_bits.append(f"{name}={value}")
    warmed = cookie
    if jar_bits:
        warmed = f"{cookie}; {'; '.join(jar_bits)}"
    meta = extract_meta_from_html(r.text or "")
    return warmed, {
        "http": r.status_code,
        "elapsed_s": round(time.time() - t0, 2),
        "cf_set": bool(jar_bits),
        "meta_len": len(meta or ""),
    }, meta


def extract_meta_from_html(html: str) -> str | None:
    for pat in (
        r'name="twitter:site-verification"\s+content="([^"]+)"',
        r'name="twitter:site―verification"\s+content="([^"]+)"',
        r'content="([^"]+)"\s+name="twitter:site-verification"',
        r'name=["\']grok-site[^"\']*verification["\'][^>]*content=["\']([^"\']+)',
    ):
        m = re.search(pat, html or "", re.I)
        if m:
            return m.group(1).strip()
    return None


def valid_sig(sig: str) -> bool:
    sig = (sig or "").strip()
    if not sig or len(sig) < 20:
        return False
    if sig.startswith("x0:") or sig.startswith("eDA6"):
        return False
    return True


def refresh_statsig(meta: str, path: str = CHAT_PATH) -> tuple[str, str]:
    payload = {"method": "POST", "path": path, "environment": {"metaContent": meta}}
    last_err = "no_signer"
    for url in SIGNER_URLS:
        try:
            r = crequests.post(url, json=payload, timeout=25)
            if r.status_code != 200:
                last_err = f"{url}:http={r.status_code}"
                continue
            value = (r.json() or {}).get("x-statsig-id") or ""
            if valid_sig(value):
                return value, url
            last_err = f"{url}:invalid_sig"
        except Exception as exc:  # noqa: BLE001
            last_err = f"{url}:{type(exc).__name__}"
    raise RuntimeError(last_err)


def ticket_age_s(ticket: dict) -> float | None:
    raw = ticket.get("signed_at")
    if not raw:
        return None
    try:
        signed_at = str(raw).replace("Z", "+00:00")
        return round(time.time() - datetime.fromisoformat(signed_at).timestamp(), 1)
    except ValueError:
        return None


def resolve_statsig(
    ticket: dict,
    *,
    proxy_url: str,
    user_agent: str,
    cookie: str,
    warm_meta: str | None,
) -> tuple[str, str]:
    chrome_sig = str(ticket.get("statsig") or ticket.get("statsigId") or "").strip()
    meta = str(ticket.get("statsig_meta") or ticket.get("statsigMeta") or "").strip() or warm_meta
    age = ticket_age_s(ticket)
    sign_source = str(ticket.get("sign_source") or "")

    if meta:
        try:
            sig, src = refresh_statsig(meta)
            return sig, f"refreshed:{src}"
        except RuntimeError as exc:
            log("statsig_refresh_warn", error=str(exc)[:200])

    if valid_sig(chrome_sig) and (
        "conversations/new" in sign_source or sign_source.startswith("turbopack:")
    ):
        if age is None or age <= 90:
            return chrome_sig, "chrome_ticket"

    if not meta:
        r = crequests.get(
            GROK_BASE + "/",
            headers={"user-agent": user_agent, "cookie": cookie, "accept": "text/html"},
            proxies={"http": proxy_url, "https": proxy_url},
            impersonate="chrome146",
            timeout=45,
        )
        meta = extract_meta_from_html(r.text or "")
    if meta:
        sig, src = refresh_statsig(meta)
        return sig, f"refreshed_late:{src}"

    if valid_sig(chrome_sig):
        return chrome_sig, "chrome_ticket_stale"
    raise RuntimeError("statsig_unavailable")


def chat_payload(message: str, *, enable_image: bool = False) -> dict:
    return {
        "temporary": True,
        "modelName": "grok-3",
        "message": message,
        "fileAttachments": [],
        "imageAttachments": [],
        "disableSearch": True,
        "enableImageGeneration": enable_image,
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "enableImageStreaming": True,
        "enableSideBySide": False,
        "sendFinalMetadata": True,
        "toolOverrides": {},
        "isPreset": False,
        "disableTextFollowUps": True,
        "modeId": "fast",
    }


def absolute_asset_url(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("https://") or value.startswith("http://"):
        return value
    return "https://assets.grok.com/" + value.lstrip("/")


def append_image_url(found: list[str], seen: set[str], raw: str) -> None:
    url = (raw or "").strip().rstrip("\\")
    if not url or url in seen:
        return
    if "-part-" in url:
        return
    if "/generated/" not in url and "assets.grok.com" not in url:
        return
    if any(ch in url for ch in "{}[]\""):
        return
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        url = absolute_asset_url(url)
    seen.add(url)
    found.append(url)


def collect_urls_from_json(value, found: list[str], seen: set[str]) -> None:
    if isinstance(value, dict):
        moderated = value.get("moderated")
        progress = value.get("progress")
        for key in ("imageUrl", "image_url", "url"):
            if key in value:
                append_image_url(found, seen, str(value.get(key) or ""))
        if moderated is False and isinstance(progress, (int, float)) and progress >= 100:
            for key in ("imageUrl", "image_url", "url"):
                append_image_url(found, seen, str(value.get(key) or ""))
        if value.get("jsonData"):
            try:
                nested = json.loads(value["jsonData"])
                collect_urls_from_json(nested, found, seen)
            except (json.JSONDecodeError, TypeError):
                pass
        for nested in value.values():
            collect_urls_from_json(nested, found, seen)
    elif isinstance(value, list):
        for item in value:
            collect_urls_from_json(item, found, seen)
    elif isinstance(value, str):
        trimmed = value.strip()
        if trimmed.startswith("{") or trimmed.startswith("["):
            try:
                collect_urls_from_json(json.loads(trimmed), found, seen)
            except json.JSONDecodeError:
                append_image_url(found, seen, trimmed)


def extract_image_urls(body: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        try:
            collect_urls_from_json(json.loads(line), found, seen)
        except json.JSONDecodeError:
            pass
    for m in re.finditer(r"https://assets\.grok\.com/[^\s\"'\\]+", body):
        append_image_url(found, seen, m.group(0))
    for m in re.finditer(r'users/[^"\s\\]+/generated/[^"\s\\]+', body):
        append_image_url(found, seen, m.group(0))
    for m in re.finditer(r'"(?:imageUrl|image_url|url)"\s*:\s*"([^"]+)"', body):
        append_image_url(found, seen, m.group(1))
    return found


def post_lite(
    proxy_url: str,
    user_agent: str,
    cookie: str,
    statsig: str,
    prompt: str,
) -> dict:
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": GROK_BASE,
        "referer": GROK_BASE + "/",
        "user-agent": user_agent,
        "cookie": cookie,
        "x-statsig-id": statsig,
        "x-xai-request-id": uuid.uuid4().hex,
    }
    proxies = {"http": proxy_url, "https": proxy_url}
    message = prompt if prompt.lower().startswith("drawing:") else f"Drawing: {prompt}"
    t0 = time.time()
    r = crequests.post(
        CHAT_URL,
        headers=headers,
        json=chat_payload(message, enable_image=True),
        proxies=proxies,
        impersonate="chrome146",
        timeout=120,
    )
    body = r.text or ""
    urls = [absolute_asset_url(u) for u in extract_image_urls(body)]
    soft_stop = '"isSoftStop":true' in body or '"isSoftStop": true' in body
    anti_bot = "Just a moment" in body or "anti-bot" in body.lower()
    system_err = re.search(r'"systemErrCode"\s*:\s*(\d+)', body)
    return {
        "http": r.status_code,
        "elapsed_s": round(time.time() - t0, 2),
        "bytes": len(body.encode("utf-8", "replace")),
        "image_urls": urls[:4],
        "soft_stop": soft_stop,
        "image_chunks": body.count("image_chunk") + body.count("imageChunk"),
        "anti_bot": anti_bot,
        "system_err_code": int(system_err.group(1)) if system_err else None,
        "body_head": body[:240],
    }


def download_asset(proxy_url: str, user_agent: str, cookie: str, url: str, dest: Path | None = None) -> dict:
    headers = {
        "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "user-agent": user_agent,
        "referer": GROK_BASE + "/",
        "origin": GROK_BASE,
        "cookie": cookie,
    }
    proxies = {"http": proxy_url, "https": proxy_url}
    t0 = time.time()
    r = crequests.get(url, headers=headers, proxies=proxies, impersonate="chrome146", timeout=60)
    body = r.content or b""
    row = {
        "http": r.status_code,
        "bytes": len(body),
        "jpeg": body[:3] == b"\xff\xd8\xff",
        "elapsed_s": round(time.time() - t0, 2),
        "url_tail": url[-80:],
    }
    if dest is not None and row["http"] == 200 and row["jpeg"]:
        dest.write_bytes(body)
        row["file"] = str(dest)
    return row


def main() -> None:
    raw = os.environ.get("TICKET_FILE", "").strip()
    if raw:
        ticket = json.loads(Path(raw).read_text(encoding="utf-8"))
    else:
        ticket = json.loads(sys.stdin.read())
    account_id = int(ticket["account_id"])
    cookie_extra = str(ticket.get("cookie") or "").strip()
    prompt = str(ticket.get("prompt") or "a red apple on white table, product photo").strip()

    sso = decrypt_sso(account_id)
    web_proxy, web_ua, web_cf = load_egress("grok_web")
    asset_proxy, asset_ua, asset_cf = load_egress("grok_web_asset")
    user_agent = str(ticket.get("user_agent") or web_ua)
    cookie = merge_cookie(sso, cookie_extra, web_cf)
    cookie, warm, warm_meta = warm_egress_cookie(web_proxy, user_agent, cookie)
    log("egress_warm", **warm)

    statsig, sig_source = resolve_statsig(
        ticket,
        proxy_url=web_proxy,
        user_agent=user_agent,
        cookie=cookie,
        warm_meta=warm_meta,
    )
    log(
        "ticket_received",
        account_id=account_id,
        statsig_len=len(statsig),
        sig_source=sig_source,
        has_cf="cf_clearance=" in cookie,
        egress_cf=bool(web_cf),
        sign_proxy_host=str(ticket.get("sign_proxy_host") or ""),
        web_proxy_host=web_proxy.split("@")[-1] if "@" in web_proxy else web_proxy,
        signed_at=ticket.get("signed_at"),
        age_s=ticket_age_s(ticket),
        has_meta=bool(ticket.get("statsig_meta") or ticket.get("statsigMeta") or warm_meta),
    )

    lite: dict = {}
    for attempt in range(1, MAX_LITE_ATTEMPTS + 1):
        if attempt > 1:
            meta = str(ticket.get("statsig_meta") or ticket.get("statsigMeta") or warm_meta or "")
            if meta:
                statsig, sig_source = refresh_statsig(meta)
                log("statsig_refresh", attempt=attempt, sig_source=sig_source, statsig_len=len(statsig))
            time.sleep(min(attempt, 3))
        lite = post_lite(web_proxy, user_agent, cookie, statsig, prompt)
        log(
            "panda_lite",
            attempt=attempt,
            sig_source=sig_source,
            **{k: lite.get(k) for k in ("http", "elapsed_s", "bytes", "soft_stop", "image_chunks", "anti_bot", "system_err_code")},
            url_count=len(lite.get("image_urls") or []),
        )
        if lite.get("system_err_code") == 1010:
            log("lite_quota_error", msg="account imagine quota exhausted or generation rejected")
            break
        if lite.get("http") == 200 and lite.get("image_urls"):
            break
        if lite.get("http") == 403 and lite.get("anti_bot"):
            meta = str(ticket.get("statsig_meta") or ticket.get("statsigMeta") or warm_meta or "")
            if meta:
                try:
                    statsig, sig_source = refresh_statsig(meta)
                    continue
                except RuntimeError as exc:
                    log("statsig_refresh_failed", error=str(exc)[:200])
            break
        if lite.get("soft_stop") and not lite.get("image_urls"):
            continue
        if lite.get("http") != 200:
            break

    downloads = []
    saved_file = ""
    OUT.mkdir(parents=True, exist_ok=True)
    dl_cookie = cookie
    for i, url in enumerate(lite.get("image_urls") or []):
        for label, proxy, ua, cf in (
            ("asset_egress", asset_proxy, asset_ua, asset_cf),
            ("web_egress", web_proxy, web_ua, web_cf),
        ):
            dest = OUT / f"ticket-lite-{account_id}-{i}.jpg"
            row = download_asset(proxy, ua, merge_cookie(sso, cookie_extra, cf) if cf else dl_cookie, url, dest)
            row["label"] = label
            downloads.append(row)
            log("download_try", **row)
            if row.get("http") == 200 and row.get("jpeg"):
                saved_file = row.get("file") or str(dest)
                break
        if saved_file:
            break

    summary = {
        "account_id": account_id,
        "sig_source": sig_source,
        "lite": lite,
        "downloads": downloads,
        "saved_file": saved_file,
        "ok": lite.get("http") == 200 and bool(lite.get("image_urls")) and bool(saved_file),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log("done", ok=summary["ok"], out=str(OUT), saved_file=saved_file or None)
    if not summary["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
