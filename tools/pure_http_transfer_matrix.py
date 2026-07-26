#!/usr/bin/env python3
"""Evidence matrix: fresh-mint vs ticket-reuse; CF/meta transfer; zero-browser extract.

Answers:
1) Do we depend on short-lived browser tickets, or mint fresh x-statsig-id?
2) Can meta / cf_clearance transfer across egress?
3) Can we bootstrap without Chrome?
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import struct
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / ".tmp" / "pure-http-grok"
CHAT_PATH = "/rest/app-chat/conversations/new"
EPOCH = 1682924400
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


def b64decode(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.b64decode(s + pad)


def generate_statsig(method: str, path: str, meta48: bytes, fingerprint: str, *, n: int | None = None, key: int | None = None) -> str:
    import random

    n = int(time.time() - EPOCH) if n is None else n
    dig = f"{method}!{path}!{n}obfiowerehiring{fingerprint}"
    sha = hashlib.sha256(dig.encode()).digest()[:16]
    key = random.randint(0, 255) if key is None else key
    block = meta48 + struct.pack("<I", n) + sha + b"\x03"
    enc = bytearray([key]) + bytearray(b ^ key for b in block)
    return base64.b64encode(bytes(enc)).decode().rstrip("=")


def decode_sig(sig: str) -> dict:
    raw = bytearray(b64decode(sig))
    key = raw[0]
    plain = bytes([key]) + bytes(b ^ key for b in raw[1:])
    n = struct.unpack("<I", plain[49:53])[0]
    return {"key": key, "n": n, "n_age_s": int(time.time() - EPOCH) - n, "meta_b64": base64.b64encode(plain[1:49]).decode(), "sha_hex": plain[53:69].hex(), "len": len(plain)}


def cookie_map(cookie: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def cookie_from(parts: dict[str, str], names: list[str]) -> str:
    return "; ".join(f"{k}={parts[k]}" for k in names if k in parts)


def chat_payload(msg: str) -> dict:
    return {
        "temporary": True,
        "modelName": None,
        "message": msg,
        "fileAttachments": [],
        "imageAttachments": [],
        "disableSearch": False,
        "enableImageGeneration": False,
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "enableImageStreaming": False,
        "imageGenerationCount": 0,
        "forceConcise": False,
        "toolOverrides": {},
        "enableSideBySide": False,
        "sendFinalMetadata": True,
        "isReasoning": False,
        "disableTextFollowUps": True,
        "responseMetadata": {},
        "disableMemory": True,
        "forceSideBySide": False,
        "isAsyncChat": False,
        "collectionIds": [],
        "modeId": "fast",
    }


def classify(status: int, body: str, headers: dict) -> str:
    low = (body or "").lower()
    if headers.get("cf-mitigated") == "challenge" or "just a moment" in low:
        return "cf_challenge"
    if status == 403 and "anti-bot" in low:
        return "anti_bot"
    if status == 200 and ("conversation" in low or "token" in low or "PURE" in body):
        return "chat_ok"
    return f"http_{status}"


def http_chat(*, proxy: str | None, cookie: str, sig: str, name: str) -> dict:
    from curl_cffi import requests as crequests

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://grok.com",
        "Referer": "https://grok.com/",
        "User-Agent": UA,
        "Cookie": cookie,
        "x-statsig-id": sig,
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    t0 = time.time()
    try:
        r = crequests.post(
            "https://grok.com" + CHAT_PATH,
            headers=headers,
            json=chat_payload("Reply with exactly: MATRIX"),
            proxies=proxies,
            impersonate="chrome146",
            timeout=60,
        )
        text = r.text or ""
        row = {
            "name": name,
            "proxy": proxy or "DIRECT",
            "http": r.status_code,
            "kind": classify(r.status_code, text, dict(r.headers)),
            "cf_mitigated": r.headers.get("cf-mitigated"),
            "elapsed_s": round(time.time() - t0, 2),
            "body_head": text[:160].replace("\n", " "),
            "sig_prefix": sig[:24],
            "sig_n": decode_sig(sig)["n"],
        }
    except Exception as exc:  # noqa: BLE001
        row = {
            "name": name,
            "proxy": proxy or "DIRECT",
            "http": 0,
            "kind": "error",
            "error": f"{type(exc).__name__}:{str(exc)[:180]}",
            "elapsed_s": round(time.time() - t0, 2),
            "sig_prefix": sig[:24],
        }
    print(json.dumps({"phase": "case", **{k: v for k, v in row.items() if k != "body_head"}, "body_head": (row.get("body_head") or "")[:80]}, ensure_ascii=False), flush=True)
    return row


def zero_browser_bootstrap(proxy: str | None) -> dict:
    """Fetch HTML verification meta via curl_cffi only; no Playwright."""
    from curl_cffi import requests as crequests

    proxies = {"http": proxy, "https": proxy} if proxy else None
    sess = crequests.Session(impersonate="chrome146", proxies=proxies)
    out: dict = {"proxy": proxy or "DIRECT"}
    try:
        ip = sess.get("https://api.ipify.org?format=json", timeout=15)
        out["egress"] = ip.json() if ip.status_code == 200 else {"http": ip.status_code}
    except Exception as exc:  # noqa: BLE001
        out["egress"] = {"error": f"{type(exc).__name__}:{str(exc)[:100]}"}
    try:
        home = sess.get("https://grok.com/", headers={"User-Agent": UA, "Accept": "text/html"}, timeout=40)
    except Exception as exc:  # noqa: BLE001
        out["home_kind"] = "error"
        out["error"] = f"{type(exc).__name__}:{str(exc)[:160]}"
        return out
    text = home.text or ""
    out["home_http"] = home.status_code
    out["home_cf"] = home.headers.get("cf-mitigated")
    out["home_kind"] = classify(home.status_code, text, dict(home.headers))
    out["set_cookie_names"] = sorted({c.split("=")[0] for c in (home.headers.get("set-cookie") or "").split(",") if "=" in c})
    try:
        jar = sess.cookies.get_dict()
        out["jar_names"] = sorted(jar.keys())
        out["jar_has_cf"] = "cf_clearance" in jar
        out["jar_cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
    except Exception:
        out["jar_names"] = []
        out["jar_cookie"] = ""
    meta = None
    for pat in (
        r'name=["\']grok-site[^"\']*verification["\'][^>]*content=["\']([^"\']+)',
        r'content=["\']([^"\']+)["\'][^>]*name=["\']grok-site[^"\']*verification',
    ):
        m = re.search(pat, text, re.I)
        if m:
            meta = m.group(1)
            break
    out["verification_len"] = len(meta) if meta else 0
    out["verification"] = meta
    if meta:
        try:
            raw = b64decode(meta)
            out["verification_bytes"] = len(raw)
        except Exception as exc:  # noqa: BLE001
            out["verification_decode_error"] = str(exc)[:80]
    (TMP / "zero_browser_home_snip.html").write_text(text[:15000], encoding="utf-8", errors="replace")
    return out


def main() -> int:
    os.environ.pop("SSL_CERT_FILE", None)
    os.environ.pop("REQUESTS_CA_BUNDLE", None)
    os.environ.pop("CURL_CA_BUNDLE", None)

    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", type=Path, default=TMP / "session_keys.json")
    ap.add_argument("--proxy", default="http://127.0.0.1:7897")
    ap.add_argument("--alt-proxy", default="", help="optional second proxy for transfer test")
    args = ap.parse_args()
    TMP.mkdir(parents=True, exist_ok=True)

    keys = json.loads(args.keys.read_text(encoding="utf-8"))
    meta = b64decode(keys["meta_b64"])
    fp = keys["fingerprint"]
    parts = cookie_map(keys["cookie"])
    extracted_at = keys.get("extracted_at")
    age_min = None
    if extracted_at:
        age_min = round((datetime.now(timezone.utc) - datetime.fromisoformat(extracted_at)).total_seconds() / 60, 1)

    # --- Q1: mint proof ---
    mint = []
    for i in range(3):
        sig = generate_statsig("POST", CHAT_PATH, meta, fp)
        d = decode_sig(sig)
        mint.append({"i": i, "sig_prefix": sig[:28], "sig_len": len(sig), **d})
        time.sleep(1.05)
    # deliberately stale counter (~10 min old) vs fresh
    n_now = int(time.time() - EPOCH)
    sig_fresh = generate_statsig("POST", CHAT_PATH, meta, fp, n=n_now, key=7)
    sig_stale = generate_statsig("POST", CHAT_PATH, meta, fp, n=n_now - 600, key=7)
    sig_ancient = generate_statsig("POST", CHAT_PATH, meta, fp, n=n_now - 86400, key=7)

    q1 = {
        "keys_age_min": age_min,
        "fingerprint": fp,
        "note": "Each request mints NEW base64; browser ticket strings are never reused. meta+fp are long-lived keys.",
        "mint_samples": mint,
        "all_sig_prefixes_unique": len({m["sig_prefix"] for m in mint}) == len(mint),
        "counters_increasing": mint[0]["n"] < mint[1]["n"] < mint[2]["n"],
    }
    print(json.dumps({"phase": "q1_mint", **{k: v for k, v in q1.items() if k != "mint_samples"}, "mint_ns": [m["n"] for m in mint]}, ensure_ascii=False), flush=True)

    full_cookie = keys["cookie"]
    sso_only = cookie_from(parts, ["sso", "sso-rw"])
    no_cf = cookie_from(parts, [k for k in parts if k not in ("cf_clearance", "__cf_bm")])
    cf_only_plus_sso = cookie_from(parts, ["sso", "sso-rw", "cf_clearance", "__cf_bm", "grok_device_id", "x-userid"])

    cases = []
    # baseline same egress as extract
    cases.append(http_chat(proxy=args.proxy, cookie=full_cookie, sig=sig_fresh, name="A_baseline_fresh_sig"))
    cases.append(http_chat(proxy=args.proxy, cookie=full_cookie, sig=sig_stale, name="B_stale_sig_10min"))
    cases.append(http_chat(proxy=args.proxy, cookie=full_cookie, sig=sig_ancient, name="C_ancient_sig_1day"))
    cases.append(http_chat(proxy=args.proxy, cookie=no_cf, sig=generate_statsig("POST", CHAT_PATH, meta, fp), name="D_no_cf_clearance"))
    cases.append(http_chat(proxy=args.proxy, cookie=sso_only, sig=generate_statsig("POST", CHAT_PATH, meta, fp), name="E_sso_only"))
    cases.append(http_chat(proxy=args.proxy, cookie=cf_only_plus_sso, sig=generate_statsig("POST", CHAT_PATH, meta, fp), name="F_sso_cf_device"))
    cases.append(http_chat(proxy=None, cookie=full_cookie, sig=generate_statsig("POST", CHAT_PATH, meta, fp), name="G_direct_full_cookie"))
    cases.append(http_chat(proxy=None, cookie=sso_only, sig=generate_statsig("POST", CHAT_PATH, meta, fp), name="H_direct_sso_only"))

    if args.alt_proxy:
        cases.append(
            http_chat(
                proxy=args.alt_proxy,
                cookie=full_cookie,
                sig=generate_statsig("POST", CHAT_PATH, meta, fp),
                name="I_alt_proxy_same_cf",
            )
        )
        cases.append(
            http_chat(
                proxy=args.alt_proxy,
                cookie=sso_only,
                sig=generate_statsig("POST", CHAT_PATH, meta, fp),
                name="J_alt_proxy_sso_only",
            )
        )

    # --- Q3: zero browser bootstrap ---
    zb_proxy = zero_browser_bootstrap(args.proxy)
    zb_direct = zero_browser_bootstrap(None)
    zb_chat = None
    if zb_proxy.get("verification") and zb_proxy.get("verification_bytes") == 48:
        meta_zb = b64decode(zb_proxy["verification"])
        # compare to extracted meta
        zb_proxy["meta_equals_extracted"] = meta_zb == meta
        cookie_zb = zb_proxy.get("jar_cookie") or ""
        # merge SSO
        if "sso=" not in cookie_zb:
            cookie_zb = (cookie_zb + "; " if cookie_zb else "") + f"sso={keys['sso']}; sso-rw={keys['sso']}"
        # try with extracted fp (animation still unknown) + zero-browser meta
        zb_chat = http_chat(
            proxy=args.proxy,
            cookie=cookie_zb,
            sig=generate_statsig("POST", CHAT_PATH, meta_zb, fp),
            name="K_zero_browser_meta_plus_extracted_fp",
        )
        # also try with empty fp
        cases.append(
            http_chat(
                proxy=args.proxy,
                cookie=cookie_zb,
                sig=generate_statsig("POST", CHAT_PATH, meta_zb, ""),
                name="L_zero_browser_meta_empty_fp",
            )
        )
        if zb_chat:
            cases.append(zb_chat)

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "q1_mint_proof": q1,
        "cases": cases,
        "zero_browser": {"via_proxy": zb_proxy, "via_direct": {k: v for k, v in zb_direct.items() if k != "verification"}},
        "summary": {
            "fresh_ok": any(c["name"] == "A_baseline_fresh_sig" and c.get("kind") == "chat_ok" for c in cases),
            "stale_10min": next((c.get("kind") for c in cases if c["name"] == "B_stale_sig_10min"), None),
            "ancient_1day": next((c.get("kind") for c in cases if c["name"] == "C_ancient_sig_1day"), None),
            "no_cf": next((c.get("kind") for c in cases if c["name"] == "D_no_cf_clearance"), None),
            "sso_only": next((c.get("kind") for c in cases if c["name"] == "E_sso_only"), None),
            "direct_full": next((c.get("kind") for c in cases if c["name"] == "G_direct_full_cookie"), None),
            "zb_home_kind": zb_proxy.get("home_kind"),
            "zb_meta_eq": zb_proxy.get("meta_equals_extracted"),
            "zb_chat": zb_chat.get("kind") if zb_chat else None,
        },
    }
    # strip huge verification from nested for size; keep length
    if "verification" in out["zero_browser"]["via_proxy"]:
        v = out["zero_browser"]["via_proxy"].pop("verification")
        out["zero_browser"]["via_proxy"]["verification_prefix"] = (v or "")[:24]
        out["zero_browser"]["via_proxy"]["jar_cookie"] = (out["zero_browser"]["via_proxy"].get("jar_cookie") or "")[:120]

    path = TMP / "transfer_matrix_result.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"phase": "done", "summary": out["summary"], "out": str(path)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
