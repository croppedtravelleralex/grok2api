#!/usr/bin/env python3
"""Zero-browser e2e with locked x_values=[45,42,40,32] on LA residential."""
from __future__ import annotations

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
from pathlib import Path
from urllib.parse import quote

from coincurve import PrivateKey
from curl_cffi import CurlMime, requests as crequests

# 出口代理从环境变量读取，禁止硬编码凭据（本仓库为公开 fork）。
# 例：GROK_EGRESS_PROXY="http://user:pass@host:port"
PROXY = os.environ.get("GROK_EGRESS_PROXY", "")
if not PROXY:
    raise SystemExit("需要设置 GROK_EGRESS_PROXY")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
SEC = '"Google Chrome";v="146", "Chromium";v="146", "Not A(Brand";v="24"'
EPOCH = 1682924400
CHAT = "/rest/app-chat/conversations/new"
XVALS = [45, 42, 40, 32]


def b64d(s: str) -> bytes:
    return base64.b64decode(s + "=" * ((4 - len(s) % 4) % 4))


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


def main() -> int:
    keys = json.loads(Path("/tmp/session_keys.json").read_text())
    sso = keys["sso"]
    sess = crequests.Session(impersonate="chrome146", proxies={"http": PROXY, "https": PROXY})
    sess.cookies.set("sso", sso, domain=".grok.com")
    sess.cookies.set("sso-rw", sso, domain=".grok.com")
    print(json.dumps({"egress": sess.get("https://api.ipify.org?format=json", timeout=20).json(), "proxy": "70.39.164.200:30000"}, ensure_ascii=False), flush=True)

    e = os.urandom(32)
    priv = PrivateKey(e)
    pub = list(priv.public_key.format(compressed=True))
    html = sess.get("https://grok.com/c", headers={"User-Agent": UA, "Accept": "text/html"}, timeout=60).text
    baggage = (re.search(r'<meta name="baggage" content="([^"]+)"', html) or [None, ""])[1]
    sentry = (re.search(r'<meta name="sentry-trace" content="([^"-]+)', html) or [None, ""])[1]
    action_js = None
    for url in re.findall(r'src="(https://cdn\.grok\.com/_next/static/chunks/[^"]+\.js)"', html):
        t = sess.get(url, timeout=30).text
        if "anonPrivateKey" in t and "createServerReference" in t:
            action_js = t
            break
    refs = re.findall(r'createServerReference\)\("([a-f0-9]+)"', action_js or "")[:3]
    assert len(refs) == 3, refs

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

    mime = CurlMime()
    mime.addpart(name="1", data=bytes(pub), filename="blob", content_type="application/octet-stream")
    mime.addpart(name="0", filename=None, data='[{"userPublicKey":"$o1"}]')
    r0 = sess.post("https://grok.com/c", headers=cheaders(refs[0]), multipart=mime, timeout=60)
    anon = re.search(r'\{"anonUserId":"([^"]+)"', r0.text or "").group(1)
    h1 = cheaders(refs[1])
    h1["content-type"] = "text/plain;charset=UTF-8"
    r1 = sess.post("https://grok.com/c", headers=h1, data=json.dumps([{"anonUserId": anon}]), timeout=60)
    hx = r1.content.hex()
    start = hx.find("3a6f38362c") + len("3a6f38362c")
    end = hx.find("313a", start)
    chal = bytes.fromhex(hx[start:end])
    sig = priv.sign_recoverable(hashlib.sha256(chal).digest(), hasher=None)[:64]
    ch = {"challenge": base64.b64encode(chal).decode(), "signature": base64.b64encode(sig).decode()}
    h2 = cheaders(refs[2])
    h2["content-type"] = "text/plain;charset=UTF-8"
    r2 = sess.post("https://grok.com/c", headers=h2, data=json.dumps([{"anonUserId": anon, **ch}]), timeout=60)
    text2 = r2.text or ""
    ver = re.search(r'grok-site[^\"]*verification\",\"content\":\"([^\"]+)\"', text2).group(1)
    meta = b64d(ver)
    anim = list(meta)[5] % 4
    st = text2.find('"curves":')
    i = text2.find("[[", st)
    depth = 0
    end = None
    for j in range(i, min(i + 80000, len(text2))):
        if text2[j] == "[":
            depth += 1
        elif text2[j] == "]":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    curves = json.loads(text2[i:end])
    svg = curves_to_svg(curves[anim])
    fpv = fingerprint(meta, svg, XVALS)
    print(json.dumps({"anim": anim, "fp": fpv, "fp_len": len(fpv), "x_values": XVALS, "ver": ver[:24]}, ensure_ascii=False), flush=True)

    cookie = "; ".join(f"{k}={v}" for k, v in sess.cookies.get_dict().items())
    if "sso=" not in cookie:
        cookie = f"sso={sso}; sso-rw={sso}; " + cookie

    def chat(name: str, *, image: bool = False) -> dict:
        sigv = gen(meta, fpv)
        msg = "Drawing: a simple cyan diamond, realistic, clear details" if image else "Reply with exactly: ZBOK"
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
        row = {"name": name, "http": r.status_code, "kind": kind, "body": text[:120]}
        print(json.dumps(row, ensure_ascii=False), flush=True)
        return row

    trow = chat("zb_text")
    irow = chat("zb_lite", image=True)
    out = {
        "pass": trow["kind"] == "chat_ok" and irow["kind"] == "image_ok",
        "text": trow["kind"],
        "lite": irow["kind"],
        "fp": fpv,
        "x_values": XVALS,
        "meta_b64": base64.b64encode(meta).decode(),
        "zero_browser": True,
        "proxy": "70.39.164.200:30000",
    }
    Path("/tmp/zb_x45_result.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    if out["pass"]:
        Path("/tmp/zb_zero_keys.json").write_text(
            json.dumps({"meta_b64": out["meta_b64"], "fingerprint": fpv, "x_values": XVALS, "sso": sso}, indent=2),
            encoding="utf-8",
        )
    print(json.dumps({"phase": "done", **{k: out[k] for k in ("pass", "text", "lite", "fp", "x_values")}}, ensure_ascii=False), flush=True)
    return 0 if out["pass"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
