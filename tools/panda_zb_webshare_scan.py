#!/usr/bin/env python3
"""Scan Webshare pool then zero-browser chat+Lite on pass_app nodes."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import random
import re
import struct
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

from coincurve import PrivateKey
from curl_cffi import CurlMime, requests as crequests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
SEC = '"Google Chrome";v="146", "Chromium";v="146", "Not A(Brand";v="24"'
EPOCH = 1682924400
CHAT = "/rest/app-chat/conversations/new"
XVALS = [45, 42, 40, 32]


def b64d(s: str) -> bytes:
    return base64.b64decode(s + "=" * ((4 - len(s) % 4) % 4))


def webshare_list(path: str = "/tmp/webshare100.txt") -> list[str]:
    out = []
    for ln in Path(path).read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split(":")
        if len(parts) >= 4:
            host, port, user, pwd = parts[0], parts[1], parts[2], ":".join(parts[3:])
            out.append(f"http://{quote(user)}:{quote(pwd)}@{host}:{port}")
    return out


def probe(proxy: str) -> dict:
    host = proxy.split("@")[-1]
    try:
        r = crequests.get(
            "https://grok.com/",
            proxies={"http": proxy, "https": proxy},
            impersonate="chrome146",
            timeout=22,
            headers={"User-Agent": UA},
        )
        text = r.text or ""
        ok = r.status_code == 200 and "<title>Grok</title>" in text and "Just a moment" not in text
        return {
            "host": host,
            "proxy": proxy,
            "http": r.status_code,
            "ok": ok,
            "cf": r.headers.get("cf-mitigated"),
            "bytes": len(text),
            "class": "pass_app" if ok else ("cf_challenge" if r.headers.get("cf-mitigated") == "challenge" or "Just a moment" in text else f"http_{r.status_code}"),
        }
    except Exception as exc:
        return {"host": host, "proxy": proxy, "ok": False, "class": "error", "error": f"{type(exc).__name__}:{str(exc)[:100]}"}


def curves_to_svg(group: list) -> str:
    parts = []
    for e in group:
        c, b = e["color"], e["bezier"]
        parts.append(f" {c[0]},{c[1]} {c[2]},{c[3]} {c[4]},{c[5]} h {e['deg']} s {b[0]},{b[1]} {b[2]},{b[3]}")
    return "M 10,30 C" + " C".join(parts)


def _h(x, param, c, e):
    f = ((x * (c - param)) / 255.0) + param
    if e:
        return math.floor(f)
    r = round(float(f), 2)
    return 0.0 if r == 0.0 else r


def cubic(t, x1, y1, x2, y2):
    def bez(u):
        o = 1 - u
        b1, b2, b3 = 3 * o * o * u, 3 * o * u * u, u * u * u
        return b1 * x1 + b2 * x2 + b3, b1 * y1 + b2 * y2 + b3

    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bez(mid)[0] < t:
            lo = mid
        else:
            hi = mid
    return bez(0.5 * (lo + hi))[1]


def xa(svg: str):
    out = []
    for part in svg[9:].split("C"):
        cleaned = re.sub(r"[^\d]+", " ", part).strip()
        out.append([0] if not cleaned else [int(x) for x in cleaned.split() if x])
    return out


def tohex(num: float) -> str:
    rounded = round(float(num), 2)
    if rounded == 0.0:
        return "0"
    sign = "-" if math.copysign(1.0, rounded) < 0 else ""
    absval = abs(rounded)
    intpart = int(math.floor(absval))
    frac = absval - intpart
    if frac == 0.0:
        return sign + format(intpart, "x")
    digits = []
    f = frac
    for _ in range(20):
        f *= 16
        d = int(math.floor(f + 1e-12))
        digits.append(format(d, "x"))
        f -= d
        if abs(f) < 1e-12:
            break
    fs = "".join(digits).rstrip("0")
    return sign + format(intpart, "x") if not fs else sign + format(intpart, "x") + "." + fs


def simulate(values, c):
    t = (round(c / 10.0) * 10) / 4096.0
    cp = [_h(v, -1 if i % 2 else 0, 1, False) for i, v in enumerate(values[7:])]
    ey = cubic(t, cp[0], cp[1], cp[2], cp[3])
    start = [float(x) for x in values[0:3]]
    end = [float(x) for x in values[3:6]]
    r = round(start[0] + (end[0] - start[0]) * ey)
    g = round(start[1] + (end[1] - start[1]) * ey)
    b = round(start[2] + (end[2] - start[2]) * ey)
    color = f"rgb({r}, {g}, {b})"
    end_angle = _h(values[6], 60, 360, True)
    rad = (end_angle * ey) * math.pi / 180.0
    cosv, sinv = math.cos(rad), math.sin(rad)

    def z(v):
        return abs(v) < 1e-7

    def integ(v):
        return abs(v - round(v)) < 1e-7

    if z(cosv):
        a = d = 0
    elif integ(cosv):
        a = d = int(round(cosv))
    else:
        a = d = f"{cosv:.6f}"
    if z(sinv):
        bval = cval = 0
    elif integ(sinv):
        bval = int(round(sinv))
        cval = int(round(-sinv))
    else:
        bval = f"{sinv:.7f}"
        cval = f"{(-sinv):.7f}"
    return color + f"matrix({a}, {bval}, {cval}, {d}, 0, 0)"


def fingerprint(meta: bytes, svg: str, xv: list) -> str:
    arr = list(meta)
    idx = arr[xv[0]] % 16
    c = ((arr[xv[1]] % 16) * (arr[xv[2]] % 16)) * (arr[xv[3]] % 16)
    concat = simulate(xa(svg)[idx], c)
    return "".join(tohex(float(m)) for m in re.findall(r"[\d.\-]+", concat)).replace(".", "").replace("-", "")


def gen(meta: bytes, fpv: str) -> str:
    n = int(time.time() - EPOCH)
    dig = f"POST!{CHAT}!{n}obfiowerehiring{fpv}"
    sha = hashlib.sha256(dig.encode()).digest()[:16]
    key = random.randint(0, 255)
    block = meta + struct.pack("<I", n) + sha + b"\x03"
    enc = bytearray([key]) + bytearray(b ^ key for b in block)
    return base64.b64encode(bytes(enc)).decode().rstrip("=")


def zero_browser_on(proxy: str, sso: str) -> dict:
    """Full challenge + chat + lite on one proxy."""
    out: dict = {"proxy_host": proxy.split("@")[-1]}
    sess = crequests.Session(impersonate="chrome146", proxies={"http": proxy, "https": proxy})
    sess.cookies.set("sso", sso, domain=".grok.com")
    sess.cookies.set("sso-rw", sso, domain=".grok.com")
    try:
        out["egress"] = sess.get("https://api.ipify.org?format=json", timeout=20).json()
    except Exception as exc:
        out["egress"] = {"error": str(exc)[:80]}

    e = os.urandom(32)
    priv = PrivateKey(e)
    pub = list(priv.public_key.format(compressed=True))
    try:
        html = sess.get("https://grok.com/c", headers={"User-Agent": UA, "Accept": "text/html"}, timeout=60).text
    except Exception as exc:
        out["error"] = f"load:{type(exc).__name__}:{str(exc)[:100]}"
        return out
    if "Just a moment" in html:
        out["error"] = "cf_challenge_on_c"
        return out
    baggage = (re.search(r'<meta name="baggage" content="([^"]+)"', html) or [None, ""])[1]
    sentry = (re.search(r'<meta name="sentry-trace" content="([^"-]+)', html) or [None, ""])[1]
    action_js = None
    for url in re.findall(r'src="(https://cdn\.grok\.com/_next/static/chunks/[^"]+\.js)"', html):
        try:
            t = sess.get(url, timeout=30).text
        except Exception:
            continue
        if "anonPrivateKey" in t and "createServerReference" in t:
            action_js = t
            break
    refs = re.findall(r'createServerReference\)\("([a-f0-9]+)"', action_js or "")[:3]
    if len(refs) < 3:
        out["error"] = "no_actions"
        return out

    def cheaders(a: str) -> dict:
        return {
            "sec-ch-ua-platform": '"Windows"',
            "next-action": a,
            "sec-ch-ua": SEC,
            "sec-ch-ua-mobile": "?0",
            "next-router-state-tree": "%5B%22%22%2C%7B%22children%22%3A%5B%22c%22%2C%7B%22children%22%3A%5B%5B%22slug%22%2C%22%22%2C%22oc%22%5D%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D",
            "baggage": baggage,
            "sentry-trace": f"{sentry}-{uuid.uuid4().hex[:16]}-0",
            "user-agent": UA,
            "accept": "text/x-component",
            "origin": "https://grok.com",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": "https://grok.com/c",
        }

    try:
        mime = CurlMime()
        mime.addpart(name="1", data=bytes(pub), filename="blob", content_type="application/octet-stream")
        mime.addpart(name="0", filename=None, data='[{"userPublicKey":"$o1"}]')
        r0 = sess.post("https://grok.com/c", headers=cheaders(refs[0]), multipart=mime, timeout=60)
        anon_m = re.search(r'\{"anonUserId":"([^"]+)"', r0.text or "")
        if not anon_m:
            out["error"] = "no_anon"
            out["r0"] = r0.status_code
            return out
        anon = anon_m.group(1)
        h1 = cheaders(refs[1])
        h1["content-type"] = "text/plain;charset=UTF-8"
        r1 = sess.post("https://grok.com/c", headers=h1, data=json.dumps([{"anonUserId": anon}]), timeout=60)
        hx = r1.content.hex()
        start = hx.find("3a6f38362c")
        if start < 0:
            out["error"] = "no_challenge"
            return out
        start += len("3a6f38362c")
        end = hx.find("313a", start)
        chal = bytes.fromhex(hx[start:end])
        sig = priv.sign_recoverable(hashlib.sha256(chal).digest(), hasher=None)[:64]
        ch = {"challenge": base64.b64encode(chal).decode(), "signature": base64.b64encode(sig).decode()}
        h2 = cheaders(refs[2])
        h2["content-type"] = "text/plain;charset=UTF-8"
        r2 = sess.post("https://grok.com/c", headers=h2, data=json.dumps([{"anonUserId": anon, **ch}]), timeout=60)
        text2 = r2.text or ""
        ver_m = re.search(r'grok-site[^\"]*verification\",\"content\":\"([^\"]+)\"', text2)
        if not ver_m:
            out["error"] = "no_ver"
            return out
        meta = b64d(ver_m.group(1))
        anim = list(meta)[5] % 4
        st = text2.find('"curves":')
        i = text2.find("[[", st)
        depth = 0
        endj = None
        for j in range(i, min(i + 80000, len(text2))):
            if text2[j] == "[":
                depth += 1
            elif text2[j] == "]":
                depth -= 1
                if depth == 0:
                    endj = j + 1
                    break
        curves = json.loads(text2[i:endj])
        svg = curves_to_svg(curves[anim])
        fpv = fingerprint(meta, svg, XVALS)
        out["fp_len"] = len(fpv)
        out["anim"] = anim
    except Exception as exc:
        out["error"] = f"challenge:{type(exc).__name__}:{str(exc)[:120]}"
        return out

    cookie = "; ".join(f"{k}={v}" for k, v in sess.cookies.get_dict().items())
    if "sso=" not in cookie:
        cookie = f"sso={sso}; sso-rw={sso}; " + cookie

    def chat(*, image: bool) -> dict:
        sigv = gen(meta, fpv)
        msg = "Drawing: a simple yellow sun, realistic, clear details" if image else "Reply with exactly: WSOK"
        r = sess.post(
            "https://grok.com" + CHAT,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://grok.com",
                "Referer": "https://grok.com/",
                "User-Agent": UA,
                "Cookie": cookie,
                "x-statsig-id": sigv,
                "x-xai-request-id": str(uuid.uuid4()),
            },
            json={
                "temporary": True,
                "message": msg,
                "fileAttachments": [],
                "imageAttachments": [],
                "enableImageGeneration": image,
                "enableImageStreaming": image,
                "returnImageBytes": False,
                "modeId": "fast",
                "disableMemory": True,
                "sendFinalMetadata": True,
                "isAsyncChat": False,
                "collectionIds": [],
                "responseMetadata": {},
                "disableSearch": False,
                "disableTextFollowUps": True,
                "imageGenerationCount": 2 if image else 0,
                "forceConcise": False,
                "enableSideBySide": False,
                "forceSideBySide": False,
                "returnRawGrokInXaiRequest": False,
                "disabledConnectorIds": [],
                "deviceEnvInfo": {
                    "darkModeEnabled": False,
                    "devicePixelRatio": 2,
                    "screenHeight": 1000,
                    "screenWidth": 1000,
                    "viewportHeight": 800,
                    "viewportWidth": 1000,
                },
            },
            timeout=120,
        )
        text = r.text or ""
        kind = (
            "image_ok"
            if image and r.status_code == 200 and ("assets.grok" in text or "generated" in text)
            else (
                "chat_ok"
                if (not image) and r.status_code == 200 and "conversation" in text
                else ("anti_bot" if "anti-bot" in text.lower() else f"http_{r.status_code}")
            )
        )
        return {"http": r.status_code, "kind": kind}

    try:
        out["text"] = chat(image=False)
        out["lite"] = chat(image=True)
        out["pass"] = out["text"].get("kind") == "chat_ok" and out["lite"].get("kind") == "image_ok"
    except Exception as exc:
        out["error"] = f"chat:{type(exc).__name__}:{str(exc)[:120]}"
        out["pass"] = False
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-n", type=int, default=100)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--try-n", type=int, default=5, help="max pass_app nodes to run zero-browser")
    ap.add_argument("--keys", default="/tmp/session_keys.json")
    ap.add_argument("--out", default="/tmp/zb_webshare_scan.json")
    args = ap.parse_args()

    keys = json.loads(Path(args.keys).read_text())
    sso = keys["sso"]
    proxies = webshare_list()[: args.scan_n]
    print(json.dumps({"phase": "scan_start", "n": len(proxies), "workers": args.workers}, ensure_ascii=False), flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(probe, p): p for p in proxies}
        for fut in as_completed(futs):
            row = fut.result()
            results.append(row)
            if row.get("ok") or row.get("class") == "cf_challenge":
                print(json.dumps({"phase": "probe", "host": row.get("host"), "class": row.get("class"), "http": row.get("http")}, ensure_ascii=False), flush=True)

    ok = [r for r in results if r.get("ok")]
    classes: dict[str, int] = {}
    for r in results:
        classes[r.get("class") or "?"] = classes.get(r.get("class") or "?", 0) + 1
    print(json.dumps({"phase": "scan_done", "ok_n": len(ok), "classes": classes}, ensure_ascii=False), flush=True)

    zb_rows = []
    for r in ok[: args.try_n]:
        print(json.dumps({"phase": "zb_start", "host": r["host"]}, ensure_ascii=False), flush=True)
        zb = zero_browser_on(r["proxy"], sso)
        zb_rows.append(zb)
        print(json.dumps({"phase": "zb_done", "host": r["host"], "pass": zb.get("pass"), "text": (zb.get("text") or {}).get("kind"), "lite": (zb.get("lite") or {}).get("kind"), "error": zb.get("error")}, ensure_ascii=False), flush=True)
        if zb.get("pass"):
            break

    out = {
        "scan": {"n": len(results), "ok_n": len(ok), "classes": classes, "ok_hosts": [r["host"] for r in ok]},
        "zero_browser": zb_rows,
        "any_pass": any(z.get("pass") for z in zb_rows),
    }
    # strip full proxy URLs with creds from saved ok list detail
    safe_results = [{k: v for k, v in r.items() if k != "proxy"} for r in results]
    out["scan"]["results"] = safe_results
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"phase": "done", "ok_n": len(ok), "any_zb_pass": out["any_pass"], "out": args.out}, ensure_ascii=False), flush=True)
    return 0 if out["any_pass"] or len(ok) == 0 else (0 if not ok else 5)


if __name__ == "__main__":
    raise SystemExit(main())
