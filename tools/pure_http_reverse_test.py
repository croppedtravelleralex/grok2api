#!/usr/bin/env python3
"""Pure-HTTP reverse + chat/Lite smoke (NO Playwright)."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from curl_cffi import requests

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / ".tmp"
PROXY = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
CHAT_URL = "https://grok.com/rest/app-chat/conversations/new"
SIGNER = "https://grok.wodf.de/sign"
IMPERSONATE = "chrome131"


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


def extract_meta(html: str) -> str | None:
    for pat in (
        r'name=["\']grok-site[^"\']*verification["\'][^>]*content=["\']([^"\']+)',
        r'content=["\']([^"\']+)["\'][^>]*name=["\']grok-site[^"\']*verification',
    ):
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1)
    return None


def classify(status: int, body: str, cf: str | None) -> str:
    lower = body.lower()
    if cf or "just a moment" in lower:
        return "cf_challenge"
    if status == 403 and "anti-bot" in lower:
        return "anti_bot_403"
    if status == 401:
        return "auth_401"
    if status == 200 and ("token" in lower or "PONG" in body or "conversation" in lower):
        return "chat_ok"
    if status == 200 and ("generated" in lower or "imageUrl" in body or "generatedImageUrls" in body):
        return "image_ok"
    if status == 200:
        return "http_200_unclear"
    return f"http_{status}"


def post_chat(sess: requests.Session, cookie: str, statsig: str | None, payload: dict) -> dict:
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
    r = sess.post(CHAT_URL, headers=headers, json=payload, timeout=90, stream=True)
    chunks: list[bytes] = []
    total = 0
    for chunk in r.iter_content(8192):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total >= 500_000:
            break
    body = b"".join(chunks).decode("utf-8", "replace")
    return {
        "http": r.status_code,
        "cf": r.headers.get("cf-mitigated"),
        "kind": classify(r.status_code, body, r.headers.get("cf-mitigated")),
        "body_prefix": body[:220].replace("\n", " "),
        "has_generated": bool(re.search(r"users/.+/generated/|generatedImageUrls|imageUrl", body)),
    }


def try_external_sign(sess: requests.Session, method: str, path: str, meta: str) -> dict:
    try:
        r = sess.post(
            SIGNER,
            json={"method": method.lower(), "path": path, "environment": {"metaContent": meta}},
            timeout=25,
            proxies=None,
        )
        data = {}
        try:
            data = r.json()
        except Exception:
            pass
        return {
            "http": r.status_code,
            "cf": r.headers.get("cf-mitigated"),
            "kind": classify(r.status_code, r.text, r.headers.get("cf-mitigated")),
            "has_sig": bool(data.get("x-statsig-id") or data.get("x_statsig_id")),
            "sig_len": len(data.get("x-statsig-id") or data.get("x_statsig_id") or ""),
            "body_prefix": r.text[:160].replace("\n", " "),
            "statsig": data.get("x-statsig-id") or data.get("x_statsig_id"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def main() -> None:
    TMP.mkdir(parents=True, exist_ok=True)
    out: dict = {"impl": "pure-http-curl_cffi", "proxy": "127.0.0.1:7897"}
    sess = requests.Session(impersonate=IMPERSONATE, proxies=PROXY)

    ip = sess.get("https://api.ipify.org?format=json", timeout=20)
    out["egress"] = ip.json() if ip.status_code == 200 else {"http": ip.status_code, "body": ip.text[:80]}
    print(json.dumps({"event": "egress", **out["egress"]}, ensure_ascii=False), flush=True)

    home = sess.get(
        "https://grok.com/",
        headers={"user-agent": UA, "accept": "text/html,application/xhtml+xml"},
        timeout=40,
    )
    meta = extract_meta(home.text) if home.status_code == 200 else None
    out["home"] = {
        "http": home.status_code,
        "cf": home.headers.get("cf-mitigated"),
        "len": len(home.content),
        "kind": classify(home.status_code, home.text, home.headers.get("cf-mitigated")),
        "title_grok": "<title>Grok</title>" in home.text,
        "meta_len": len(meta) if meta else 0,
        "meta_prefix": (meta or "")[:24],
    }
    print(json.dumps({"event": "home", **out["home"]}, ensure_ascii=False), flush=True)
    (TMP / "grok_home_snip.html").write_text(home.text[:12000], encoding="utf-8", errors="replace")

    chunks = sorted(set(re.findall(r"https://cdn\.grok\.com/_next/static/chunks/[^\s\"']+\.js", home.text)))
    hits = []
    if home.status_code == 200 and chunks:
        for url in chunks[:80]:
            try:
                t = sess.get(url, headers={"user-agent": UA}, timeout=20).text
            except Exception:
                continue
            keys = [k for k in ("x-statsig-id", "statsig", "childNodes", "grok-site", "verification") if k in t]
            if keys:
                idx = max(t.find("x-statsig"), t.find("statsig"), t.find("childNodes"), 0)
                hits.append({"url_tail": url[-100:], "keys": keys, "size": len(t), "snip": t[max(0, idx - 60) : idx + 140].replace("\n", " ")[:200]})
    out["chunks"] = {"count": len(chunks), "signer_hits": hits[:15]}
    print(json.dumps({"event": "chunks", "count": len(chunks), "hits": len(hits)}, ensure_ascii=False), flush=True)

    # external signer
    if meta:
        out["external_sign"] = try_external_sign(sess, "POST", "/rest/app-chat/conversations/new", meta)
    else:
        out["external_sign"] = {"skipped": True, "reason": "no meta (likely CF)"}
    print(json.dumps({"event": "external_sign", **{k: v for k, v in out["external_sign"].items() if k != "statsig"}}, ensure_ascii=False), flush=True)

    # SSO tests
    sso_path = TMP / "web-sso-canary.json"
    if not sso_path.exists():
        out["chat"] = {"skipped": True, "reason": "no .tmp/web-sso-canary.json"}
        print(json.dumps({"event": "done", "path": str(TMP / "pure_http_reverse_result.json")}, ensure_ascii=False))
        (TMP / "pure_http_reverse_result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        raise SystemExit(2)

    acc = next(a for a in json.loads(sso_path.read_text(encoding="utf-8"))["accounts"] if a.get("sso"))
    token = acc["sso"].strip()
    if token.lower().startswith("sso="):
        token = token[4:].strip()
    cookie = f"sso={token}; sso-rw={token}"
    out["account_id"] = acc["id"]

    a = post_chat(sess, cookie, None, chat_payload("Reply with exactly: PONG"))
    out["A_no_sig"] = a
    print(json.dumps({"event": "A_no_sig", "account_id": acc["id"], **{k: a[k] for k in ("http", "kind", "cf")}}, ensure_ascii=False), flush=True)

    sig = out.get("external_sign", {}).get("statsig")
    if sig:
        c = post_chat(sess, cookie, sig, chat_payload("Reply with exactly: PONG"))
        out["C_signed_chat"] = {k: c[k] for k in c if k != "statsig"}
        print(json.dumps({"event": "C_signed_chat", **{k: c[k] for k in ("http", "kind", "cf")}}, ensure_ascii=False), flush=True)
        d = post_chat(
            sess,
            cookie,
            sig,
            chat_payload("Drawing: a simple red apple on white background, minimal", enable_image=True),
        )
        out["D_lite"] = d
        print(
            json.dumps(
                {"event": "D_lite", **{k: d[k] for k in ("http", "kind", "cf", "has_generated")}},
                ensure_ascii=False,
            ),
            flush=True,
        )
    else:
        out["C_signed_chat"] = {"skipped": True, "reason": "no statsig from pure-HTTP signer"}
        out["D_lite"] = {"skipped": True, "reason": "no statsig"}
        print(json.dumps({"event": "C_signed_chat", "skipped": True, "reason": "no statsig"}), flush=True)

    path = TMP / "pure_http_reverse_result.json"
    # strip secret sig from file? keep only length
    if out.get("external_sign", {}).get("statsig"):
        out["external_sign"]["statsig_len"] = len(out["external_sign"].pop("statsig"))
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "event": "acceptance",
                "cf_pass": out["home"].get("kind") != "cf_challenge" and out["home"].get("http") == 200,
                "A_anti_bot": out.get("A_no_sig", {}).get("kind") == "anti_bot_403",
                "C_chat_ok": out.get("C_signed_chat", {}).get("kind") == "chat_ok",
                "D_image_ok": out.get("D_lite", {}).get("kind") == "image_ok" or out.get("D_lite", {}).get("has_generated"),
                "path": str(path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
