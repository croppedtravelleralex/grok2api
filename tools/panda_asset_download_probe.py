#!/usr/bin/env python3
"""Probe assets.grok.com download via udeal vs other egress paths.

Runs on Panda. For each candidate account:
  1) Sign + Lite SSE to obtain a real assets.grok.com URL
  2) Download that URL through a matrix of proxy/cookie combinations

Output: JSON lines + summary file under /tmp/panda-asset-probe-*.
"""
from __future__ import annotations

import base64
import json
import re
import sqlite3
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:
    from curl_cffi import requests as crequests
except ImportError:
    print("curl_cffi required", file=sys.stderr)
    raise SystemExit(2)

DB = Path("/opt/grok2api/data/backend.db")
CONFIG = Path("/opt/grok2api/config.yaml")
BRIDGE_KEY = Path("/root/.secrets/grok2api-browser-bridge-key-main")
BRIDGE_NAME = "grok2api-browser-bridge"
UDEAL_LIST = Path("/tmp/udeal1000proxy.txt")
UDEAL_PASSWORD = "IM0aSd"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
CHAT_PATH = "/rest/app-chat/conversations/new"
OUT = Path(f"/tmp/panda-asset-probe-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
DEFAULT_ACCOUNTS = [241, 1385, 240, 235, 337, 339, 446]


def log(event: str, **kw) -> None:
    row = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **kw}
    line = json.dumps(row, ensure_ascii=False)
    print(line, flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_key() -> bytes:
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        if "credentialEncryptionKey" in line:
            return base64.b64decode(line.split(":", 1)[1].strip().strip("\"'"))
    raise SystemExit("credentialEncryptionKey missing")


def decrypt_blob(aes: AESGCM, blob: str) -> str:
    text = (blob or "").strip()
    if not text:
        return ""
    pad = "=" * ((4 - len(text) % 4) % 4)
    raw = base64.b64decode(text + pad)
    return aes.decrypt(raw[:12], raw[12:], None).decode()


def decrypt_sso(aes: AESGCM, account_id: int) -> str:
    row = sqlite3.connect(DB).execute(
        "SELECT encrypted_primary FROM account_credentials WHERE account_id=?",
        (account_id,),
    ).fetchone()
    if not row:
        raise RuntimeError(f"no credentials for account {account_id}")
    token = decrypt_blob(aes, row[0]).strip()
    if token.lower().startswith("sso="):
        token = token[4:].strip()
    return token


def load_egress_nodes(aes: AESGCM) -> dict[str, dict]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    out: dict[str, dict] = {}
    for scope in ("grok_web", "grok_web_asset"):
        row = con.execute(
            """
            SELECT id, name, scope, enabled, health, encrypted_proxy_url,
                   encrypted_cloudflare_cookie, user_agent, last_error
            FROM egress_nodes
            WHERE scope=? AND enabled=1
            ORDER BY health DESC, id ASC
            LIMIT 1
            """,
            (scope,),
        ).fetchone()
        if not row:
            continue
        out[scope] = {
            "id": row["id"],
            "name": row["name"],
            "scope": row["scope"],
            "health": row["health"],
            "proxy_url": decrypt_blob(aes, row["encrypted_proxy_url"]),
            "cf_cookies": decrypt_blob(aes, row["encrypted_cloudflare_cookie"]),
            "user_agent": (row["user_agent"] or "").strip() or UA,
            "last_error": row["last_error"] or "",
        }
    return out


def udeal_proxy(session: str) -> str:
    user = f"userId-2684-custom-8241-region-sg-session-{session}-sessTime-15"
    return f"http://{quote(user, safe='')}:{quote(UDEAL_PASSWORD, safe='')}@as.udealproxy.com:6666"


def sessions_from_udeal(limit: int = 20) -> list[str]:
    out: list[str] = []
    if not UDEAL_LIST.exists():
        return out
    for line in UDEAL_LIST.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        if "session=" not in line:
            continue
        session = (dict(x.split("=", 1) for x in urlsplit(line.strip()).query.split("&") if "=" in x).get("session") or "").strip()
        if session:
            out.append(session)
    return list(dict.fromkeys(out))[:limit]


def pick_udeal_session() -> str:
    sessions = sessions_from_udeal(1)
    if not sessions:
        raise SystemExit("no udeal session in /tmp/udeal1000proxy.txt")
    return sessions[0]


def cookie_header(sso: str, cf: str = "") -> str:
    parts = [f"sso={sso}", f"sso-rw={sso}"]
    cf = (cf or "").strip().strip(";")
    if cf:
        parts.append(cf)
    return "; ".join(parts)


def bridge_sign(sso: str, proxy_url: str) -> dict:
    session_key = f"asset-probe-{uuid.uuid4().hex[:8]}"
    payload = {
        "path": "/rest/modes",
        "method": "GET",
        "cookie": cookie_header(sso),
        "userAgent": UA,
        "referer": "https://grok.com/",
        "sessionKey": session_key,
        "timeoutMs": 120000,
        "proxyUrl": proxy_url,
    }
    inner = r"""
import json,sys,urllib.error,urllib.request
payload=json.load(sys.stdin)
key=open('/run/secrets/browser-bridge-key','rb').read().strip().decode()
body=json.dumps(payload).encode()
req=urllib.request.Request(
  'http://127.0.0.1:8192/v1/sign', data=body,
  headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'}, method='POST')
try:
  with urllib.request.urlopen(req, timeout=150) as resp:
    print(json.dumps({'http':resp.status,'body':json.loads(resp.read().decode())}))
except urllib.error.HTTPError as e:
  err=e.read().decode('utf-8','replace')
  try: parsed=json.loads(err)
  except Exception: parsed=None
  print(json.dumps({'http':e.code,'error':str(e),'body':parsed,'body_text':err[:800]}))
except Exception as e:
  print(json.dumps({'http':0,'error':type(e).__name__+': '+str(e)[:400]}))
"""
    proc = subprocess.run(
        ["docker", "exec", "-i", BRIDGE_NAME, "python3", "-c", inner],
        input=json.dumps(payload).encode(),
        capture_output=True,
        timeout=180,
    )
    stdout = (proc.stdout or b"").decode("utf-8", "replace").strip()
    lines = [ln for ln in stdout.splitlines() if ln.startswith("{")]
    if not lines:
        return {"http": 0, "error": "empty_sign_stdout", "stderr": (proc.stderr or b"").decode()[:300]}
    data = json.loads(lines[-1])
    body = data.get("body") if isinstance(data.get("body"), dict) else {}
    cookie = body.get("cookie") or body.get("cookies") or body.get("cookieHeader") or ""
    statsig = body.get("statsigId") or ""
    return {
        "http": data.get("http", 0),
        "statsig": statsig,
        "statsig_len": len(statsig),
        "cookie": cookie,
        "has_cf": "cf_clearance=" in cookie,
        "error": (body.get("error") or data.get("error") or "")[:300],
        "session_key": session_key,
    }


def lite_asset_urls(proxy_url: str, sso: str, statsig: str, cookie: str) -> dict:
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Origin": "https://grok.com",
        "Referer": "https://grok.com/",
        "Content-Type": "application/json",
        "Cookie": cookie if "sso=" in cookie else cookie_header(sso, cookie),
    }
    if statsig:
        headers["x-statsig-id"] = statsig
    payload = {
        "temporary": True,
        "modelName": "grok-3",
        "message": "Drawing: a red apple on white table, product photo",
        "fileAttachments": [],
        "imageAttachments": [],
        "disableSearch": True,
        "enableImageGeneration": True,
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
    proxies = {"http": proxy_url, "https": proxy_url}
    t0 = time.time()
    try:
        r = crequests.post(
            "https://grok.com" + CHAT_PATH,
            headers=headers,
            json=payload,
            proxies=proxies,
            impersonate="chrome146",
            timeout=120,
        )
        text = r.text or ""
        urls = list(dict.fromkeys(re.findall(r"https://assets\.grok\.com/[^\s\"'\\]+", text)))
        soft_stop = '"isSoftStop":true' in text or '"isSoftStop": true' in text
        image_chunks = text.count("image_chunk") + text.count("imageChunk")
        return {
            "http": r.status_code,
            "elapsed_s": round(time.time() - t0, 2),
            "bytes": len(r.content or b""),
            "asset_urls": urls[:4],
            "soft_stop": soft_stop,
            "image_chunks": image_chunks,
            "anti_bot": "Just a moment" in text,
            "body_head": text[:240],
        }
    except Exception as exc:
        return {"http": 0, "elapsed_s": round(time.time() - t0, 2), "error": f"{type(exc).__name__}:{exc}"[:300]}


def probe_download(label: str, url: str, proxy_url: str | None, cookie: str, user_agent: str) -> dict:
    headers = {
        "User-Agent": user_agent,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": "https://grok.com/",
        "Origin": "https://grok.com",
        "Cookie": cookie,
    }
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    t0 = time.time()
    try:
        r = crequests.get(url, headers=headers, proxies=proxies, impersonate="chrome146", timeout=60)
        body = r.content or b""
        return {
            "label": label,
            "http": r.status_code,
            "bytes": len(body),
            "jpeg": body[:3] == b"\xff\xd8\xff",
            "webp": body[:4] == b"RIFF" and body[8:12] == b"WEBP",
            "content_type": r.headers.get("content-type", ""),
            "elapsed_s": round(time.time() - t0, 2),
            "proxy": (proxy_url.split("@")[-1] if proxy_url and "@" in proxy_url else proxy_url or "direct"),
            "body_head": body[:120].decode("utf-8", "replace"),
        }
    except Exception as exc:
        return {
            "label": label,
            "http": 0,
            "error": f"{type(exc).__name__}:{exc}"[:300],
            "elapsed_s": round(time.time() - t0, 2),
            "proxy": (proxy_url.split("@")[-1] if proxy_url and "@" in proxy_url else proxy_url or "direct"),
        }


def download_matrix(url: str, sso: str, egress: dict[str, dict], sign_cookie: str) -> list[dict]:
    web = egress.get("grok_web") or {}
    asset = egress.get("grok_web_asset") or {}
    alt_udeal = udeal_proxy(pick_udeal_session())
    rows = []
    combos = [
        ("asset_db_sso_only", asset.get("proxy_url"), cookie_header(sso), asset.get("user_agent") or UA),
        ("asset_db_sso_cf", asset.get("proxy_url"), cookie_header(sso, asset.get("cf_cookies", "")), asset.get("user_agent") or UA),
        ("web_db_sso_cf", web.get("proxy_url"), cookie_header(sso, web.get("cf_cookies", "")), web.get("user_agent") or UA),
        ("asset_db_sign_cookie", asset.get("proxy_url"), sign_cookie or cookie_header(sso), asset.get("user_agent") or UA),
        ("web_db_sign_cookie", web.get("proxy_url"), sign_cookie or cookie_header(sso), web.get("user_agent") or UA),
        ("alt_udeal_sso_only", alt_udeal, cookie_header(sso), UA),
        ("alt_udeal_sign_cookie", alt_udeal, sign_cookie or cookie_header(sso), UA),
        ("direct_sso_only", None, cookie_header(sso), UA),
        ("direct_sign_cookie", None, sign_cookie or cookie_header(sso), UA),
        ("direct_no_cookie", None, "", UA),
    ]
    for label, proxy, cookie, ua in combos:
        if label.startswith("asset_") and not asset.get("proxy_url"):
            continue
        if label.startswith("web_") and not web.get("proxy_url"):
            continue
        rows.append(probe_download(label, url, proxy, cookie, ua))
    return rows


SIGNER_URL = "https://grok.wodf.de/sign"
GROK_BASE = "https://grok.com"


def remote_statsig(proxy_url: str, sso: str, cf: str, path: str = CHAT_PATH, user_agent: str = UA) -> str:
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cookie": cookie_header(sso, cf),
    }
    proxies = {"http": proxy_url, "https": proxy_url}
    page = crequests.get(f"{GROK_BASE}/", headers=headers, proxies=proxies, impersonate="chrome146", timeout=45)
    if page.status_code != 200:
        raise RuntimeError(f"meta_page_http={page.status_code}")
    m = re.search(r'name="twitter:site-verification"\s+content="([^"]+)"', page.text or "")
    if not m:
        raise RuntimeError("meta_content_missing")
    payload = {
        "method": "POST",
        "path": path,
        "environment": {"metaContent": m.group(1)},
    }
    sign = crequests.post(SIGNER_URL, json=payload, timeout=20)
    if sign.status_code != 200:
        raise RuntimeError(f"signer_http={sign.status_code}")
    value = sign.json().get("x-statsig-id") or ""
    if len(value) < 20:
        raise RuntimeError("signer_invalid")
    return value


def obtain_url_remote(account_id: int, aes: AESGCM, egress: dict[str, dict], proxies: list[str]) -> tuple[dict, str, dict]:
    sso = decrypt_sso(aes, account_id)
    web = egress.get("grok_web") or {}
    cf = web.get("cf_cookies", "")
    ua = web.get("user_agent") or UA
    last_err = "no_proxy_attempt"
    for proxy in proxies[:6]:
        try:
            statsig = remote_statsig(proxy, sso, cf, user_agent=ua)
            cookie = cookie_header(sso, cf)
            lite = lite_asset_urls(proxy, sso, statsig, cookie)
            urls = lite.get("asset_urls") or []
            if urls:
                return {"statsig_len": len(statsig), "has_cf": "cf_clearance=" in cookie, "proxy_host": proxy.split("@")[-1]}, urls[0], lite
            last_err = json.dumps({"lite": {k: lite.get(k) for k in ("http", "soft_stop", "image_chunks")}}, ensure_ascii=False)
        except Exception as exc:
            last_err = str(exc)[:300]
    raise RuntimeError(last_err)


def obtain_url_via_grok2api(prompt: str = "a red apple on white table") -> tuple[int | None, str, dict]:
    key_path = Path("/root/.secrets/grok2api-newapi-key")
    if not key_path.exists():
        raise RuntimeError("client key missing")
    api_key = key_path.read_text(encoding="utf-8").strip()
    body = json.dumps(
        {"model": "grok-imagine-image", "prompt": prompt, "n": 1, "response_format": "url"},
        ensure_ascii=False,
    ).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:18000/v1/images/generations",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            request_id = resp.headers.get("x-request-id") or resp.headers.get("X-Request-Id") or ""
            payload = json.loads(resp.read().decode("utf-8", "replace"))
            data = payload.get("data") or []
            url = ""
            if data and isinstance(data[0], dict):
                url = str(data[0].get("url") or "")
            account_id = lookup_audit_account(request_id)
            return account_id, url, {
                "http": resp.status,
                "elapsed_s": round(time.time() - t0, 2),
                "source": "grok2api_ok",
                "request_id": request_id,
                "account_id": account_id,
            }
    except urllib.error.HTTPError as exc:
        request_id = exc.headers.get("x-request-id") or exc.headers.get("X-Request-Id") or ""
        text = exc.read().decode("utf-8", "replace")
        account_id = lookup_audit_account(request_id)
        return account_id, "", {
            "http": exc.code,
            "elapsed_s": round(time.time() - t0, 2),
            "source": "grok2api_fail",
            "request_id": request_id,
            "account_id": account_id,
            "body": text[:400],
        }


def lookup_audit_account(request_id: str) -> int | None:
    rid = (request_id or "").strip()
    if not rid:
        return None
    row = sqlite3.connect(DB).execute(
        """
        SELECT pa.id
        FROM request_audits ra
        JOIN model_routes mr ON mr.id = ra.model_route_id
        JOIN provider_accounts pa ON pa.id = (
            SELECT account_id FROM image_pipeline_traces ipt WHERE ipt.request_id = ra.request_id LIMIT 1
        )
        WHERE ra.request_id = ?
        LIMIT 1
        """,
        (rid,),
    ).fetchone()
    if row and row[0]:
        return int(row[0])
    row = sqlite3.connect(DB).execute(
        "SELECT account_id FROM image_pipeline_traces WHERE request_id=? AND account_id IS NOT NULL LIMIT 1",
        (rid,),
    ).fetchone()
    return int(row[0]) if row and row[0] else None


def obtain_lite_url(sso: str, sign_proxies: list[str], max_proxies: int = 3) -> tuple[dict, str, dict]:
    last: dict = {"http": 0, "error": "no_sign_attempt"}
    for proxy in sign_proxies[:max_proxies]:
        sign = bridge_sign(sso, proxy)
        last = sign
        if not sign.get("statsig"):
            last = {**sign, "error": f"sign_failed:{sign.get('error')}"}
            continue
        lite = lite_asset_urls(proxy, sso, sign["statsig"], sign.get("cookie") or cookie_header(sso))
        urls = lite.get("asset_urls") or []
        if urls:
            return sign, urls[0], lite
        last = {**sign, "lite": {k: lite.get(k) for k in ("http", "soft_stop", "image_chunks", "anti_bot")}, "error": "no_asset_url"}
    raise RuntimeError(json.dumps(last, ensure_ascii=False)[:600])


def sign_proxy_candidates(egress: dict[str, dict]) -> list[str]:
    out: list[str] = []
    for scope in ("grok_web", "grok_web_asset"):
        proxy = (egress.get(scope) or {}).get("proxy_url")
        if proxy and proxy not in out:
            out.append(proxy)
    for session in sessions_from_udeal(8):
        proxy = udeal_proxy(session)
        if proxy not in out:
            out.append(proxy)
    return out


def candidate_accounts() -> list[int]:
    env = [int(x) for x in (__import__("os").environ.get("PROBE_ACCOUNTS") or "").split(",") if x.strip().isdigit()]
    if env:
        return env
    rows = sqlite3.connect(DB).execute(
        """
        SELECT DISTINCT a.id
        FROM provider_accounts a
        JOIN account_quota_windows w ON w.account_id = a.id
        WHERE a.provider='grok_web' AND a.enabled=1 AND a.auth_status='active'
          AND w.mode='imagine' AND w.remaining > 0
        ORDER BY a.id DESC
        LIMIT 20
        """
    ).fetchall()
    ordered = []
    for aid in DEFAULT_ACCOUNTS:
        if aid not in ordered:
            ordered.append(aid)
    for (aid,) in rows:
        if aid not in ordered:
            ordered.append(int(aid))
    return ordered[:12]


def main() -> None:
    import os

    key = load_key()
    aes = AESGCM(key)
    egress = load_egress_nodes(aes)
    log("egress", nodes={k: {"id": v["id"], "name": v["name"], "proxy_host": v["proxy_url"].split("@")[-1]} for k, v in egress.items()})
    sign_proxies = sign_proxy_candidates(egress)
    log("sign_proxies", count=len(sign_proxies), hosts=[p.split("@")[-1] if "@" in p else p for p in sign_proxies[:6]])
    summary: dict = {"accounts": [], "egress": {k: v["name"] for k, v in egress.items()}}

    asset_url_override = os.environ.get("ASSET_URL", "").strip()
    asset_account_override = int(os.environ.get("ASSET_ACCOUNT_ID", "0") or "0")

    if asset_url_override and asset_account_override:
        sso = decrypt_sso(aes, asset_account_override)
        row = {
            "account_id": asset_account_override,
            "asset_url_tail": asset_url_override[-80:],
            "mode": "download_only",
            "downloads": download_matrix(asset_url_override, sso, egress, cookie_header(sso)),
        }
        row["download_ok"] = [d["label"] for d in row["downloads"] if d.get("http") == 200 and (d.get("jpeg") or d.get("webp"))]
        row["download_403"] = [d["label"] for d in row["downloads"] if d.get("http") == 403]
        summary["accounts"].append(row)
        log("download_only_done", account_id=asset_account_override, ok=row["download_ok"], forbidden=row["download_403"])
    else:
        grok_account_id = int(os.environ.get("GROK2API_ACCOUNT_ID", "0") or "0")
        grok_url = ""
        grok_meta: dict = {}
        if os.environ.get("USE_GROK2API_URL", "1").strip().lower() not in {"0", "false", "no"}:
            grok_account_id, grok_url, grok_meta = obtain_url_via_grok2api()
            log("grok2api_obtain", meta=grok_meta, has_url=bool(grok_url))
        if grok_url:
            account_id = grok_account_id or int(os.environ.get("FALLBACK_ACCOUNT_ID", "1385"))
            sso = decrypt_sso(aes, account_id)
            row = {
                "account_id": account_id,
                "mode": "grok2api_url",
                "grok2api": grok_meta,
                "asset_url_tail": grok_url[-80:],
                "downloads": download_matrix(grok_url, sso, egress, cookie_header(sso)),
            }
            row["download_ok"] = [d["label"] for d in row["downloads"] if d.get("http") == 200 and (d.get("jpeg") or d.get("webp"))]
            row["download_403"] = [d["label"] for d in row["downloads"] if d.get("http") == 403]
            summary["accounts"].append(row)
            log("grok2api_done", account_id=account_id, ok=row["download_ok"], forbidden=row["download_403"])
        elif os.environ.get("REMOTE_ACCOUNT_ID", "").strip().isdigit():
            account_id = int(os.environ["REMOTE_ACCOUNT_ID"])
            sign, url, lite = obtain_url_remote(account_id, aes, egress, sign_proxies)
            sso = decrypt_sso(aes, account_id)
            row = {
                "account_id": account_id,
                "mode": "remote_sign_lite",
                "sign": sign,
                "lite": {k: lite.get(k) for k in ("http", "elapsed_s", "bytes", "soft_stop", "image_chunks", "anti_bot")},
                "asset_url_tail": url[-80:],
                "downloads": download_matrix(url, sso, egress, cookie_header(sso, (egress.get("grok_web") or {}).get("cf_cookies", ""))),
            }
            row["download_ok"] = [d["label"] for d in row["downloads"] if d.get("http") == 200 and (d.get("jpeg") or d.get("webp"))]
            row["download_403"] = [d["label"] for d in row["downloads"] if d.get("http") == 403]
            summary["accounts"].append(row)
            log("remote_done", account_id=account_id, ok=row["download_ok"], forbidden=row["download_403"])
        elif os.environ.get("USE_LITE_URL", "0").strip().lower() in {"1", "true", "yes"}:
            for account_id in candidate_accounts():
                row = {"account_id": account_id}
                try:
                    sso = decrypt_sso(aes, account_id)
                    sign, url, lite = obtain_lite_url(sso, sign_proxies)
                except Exception as exc:
                    row["error"] = str(exc)[:600]
                    summary["accounts"].append(row)
                    log("account_skip", **row)
                    continue
                row["sign_http"] = sign.get("http")
                row["sign_has_cf"] = sign.get("has_cf")
                row["lite"] = {k: lite.get(k) for k in ("http", "elapsed_s", "bytes", "soft_stop", "image_chunks", "anti_bot")}
                row["asset_url_tail"] = url[-80:]
                downloads = download_matrix(url, sso, egress, sign.get("cookie") or "")
                row["downloads"] = downloads
                ok = [d for d in downloads if d.get("http") == 200 and (d.get("jpeg") or d.get("webp"))]
                row["download_ok"] = [d["label"] for d in ok]
                row["download_403"] = [d["label"] for d in downloads if d.get("http") == 403]
                summary["accounts"].append(row)
                log("account_done", account_id=account_id, url_tail=row["asset_url_tail"], ok=row["download_ok"], forbidden=row["download_403"])
                if ok or row["download_403"]:
                    break
    summary["verdict"] = (
        "asset_403_reproduced"
        if any(a.get("download_403") for a in summary["accounts"])
        else ("no_url_obtained" if not any(a.get("downloads") for a in summary["accounts"]) else "downloads_ok")
    )
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log("done", out=str(OUT), verdict=summary["verdict"])
    print(f"summary={OUT / 'summary.json'}", flush=True)


if __name__ == "__main__":
    main()
