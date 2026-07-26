#!/usr/bin/env python3
"""Zero-browser on LA residential: rebuild SVG from curves → XCTID fp → chat+Lite."""
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

# User-specified egress (LA residential)
# 出口代理从环境变量读取，禁止硬编码凭据（本仓库为公开 fork）。
# 例：GROK_EGRESS_PROXY="http://user:pass@host:port"
PROXY = os.environ.get("GROK_EGRESS_PROXY", "")
if not PROXY:
    raise SystemExit("需要设置 GROK_EGRESS_PROXY")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
SEC = '"Google Chrome";v="146", "Chromium";v="146", "Not A(Brand";v="24"'
EPOCH = 1682924400
CHAT = "/rest/app-chat/conversations/new"


def b64d(s: str) -> bytes:
    return base64.b64decode(s + "=" * ((4 - len(s) % 4) % 4))


def curves_to_svg(group: list[dict]) -> str:
    """Mirror site JS: M 10,30 C ${color...} h ${deg} s ${bezier...}"""
    parts = []
    for e in group:
        c = e["color"]
        b = e["bezier"]
        parts.append(
            f" {c[0]},{c[1]} {c[2]},{c[3]} {c[4]},{c[5]} h {e['deg']} s {b[0]},{b[1]} {b[2]},{b[3]}"
        )
    return "M 10,30 C" + " C".join(parts)


def _h(x, param, c, e):
    f = ((x * (c - param)) / 255.0) + param
    if e:
        return math.floor(f)
    rounded = round(float(f), 2)
    return 0.0 if rounded == 0.0 else rounded


def cubic_bezier_eased(t, x1, y1, x2, y2):
    def bezier(u):
        omu = 1.0 - u
        b1 = 3.0 * omu * omu * u
        b2 = 3.0 * omu * u * u
        b3 = u * u * u
        return b1 * x1 + b2 * x2 + b3, b1 * y1 + b2 * y2 + b3

    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bezier(mid)[0] < t:
            lo = mid
        else:
            hi = mid
    return bezier(0.5 * (lo + hi))[1]


def xa(svg: str):
    out = []
    for part in svg[9:].split("C"):
        cleaned = re.sub(r"[^\d]+", " ", part).strip()
        out.append([0] if cleaned == "" else [int(tok) for tok in cleaned.split() if tok])
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
    frac_digits = []
    f = frac
    for _ in range(20):
        f *= 16
        digit = int(math.floor(f + 1e-12))
        frac_digits.append(format(digit, "x"))
        f -= digit
        if abs(f) < 1e-12:
            break
    frac_str = "".join(frac_digits).rstrip("0")
    return sign + format(intpart, "x") if frac_str == "" else sign + format(intpart, "x") + "." + frac_str


def simulate_style(values, c):
    t = (round(c / 10.0) * 10) / 4096.0
    cp = [_h(v, -1 if (i % 2) else 0, 1, False) for i, v in enumerate(values[7:])]
    eased_y = cubic_bezier_eased(t, cp[0], cp[1], cp[2], cp[3])
    start = [float(x) for x in values[0:3]]
    end = [float(x) for x in values[3:6]]
    r = round(start[0] + (end[0] - start[0]) * eased_y)
    g = round(start[1] + (end[1] - start[1]) * eased_y)
    b = round(start[2] + (end[2] - start[2]) * eased_y)
    color = f"rgb({r}, {g}, {b})"
    end_angle = _h(values[6], 60, 360, True)
    rad = (end_angle * eased_y) * math.pi / 180.0
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
    return {"color": color, "transform": f"matrix({a}, {bval}, {cval}, {d}, 0, 0)"}


def fingerprint(meta: bytes, svg: str, x_values: list) -> str:
    arr = list(meta)
    idx = arr[x_values[0]] % 16
    c = ((arr[x_values[1]] % 16) * (arr[x_values[2]] % 16)) * (arr[x_values[3]] % 16)
    vals = xa(svg)[idx]
    k = simulate_style(vals, c)
    concat = str(k["color"]) + str(k["transform"])
    return "".join(tohex(float(m)) for m in re.findall(r"[\d.\-]+", concat)).replace(".", "").replace("-", "")


def generate_statsig(method: str, path: str, meta: bytes, fp: str) -> str:
    n = int(time.time() - EPOCH)
    dig = f"{method}!{path}!{n}obfiowerehiring{fp}"
    sha = hashlib.sha256(dig.encode()).digest()[:16]
    key = random.randint(0, 255)
    block = meta + struct.pack("<I", n) + sha + b"\x03"
    enc = bytearray([key]) + bytearray(b ^ key for b in block)
    return base64.b64encode(bytes(enc)).decode().rstrip("=")


def discover_x_values(sess) -> list[int]:
    """Pull turbopack module / chunks for x[N],16 indices."""
    # Known historical + scan signer-related chunks
    html = sess.get("https://grok.com/", timeout=60).text
    scripts = re.findall(r'src="(https://cdn\.grok\.com/_next/static/chunks/[^"]+\.js)"', html)
    for url in scripts:
        try:
            t = sess.get(url, timeout=25).text
        except Exception:
            continue
        nums = [int(x) for x in re.findall(r"x\[(\d+)\]\s*,\s*16", t)]
        if len(nums) >= 4:
            return nums[:4]
        # alternate obfuscation: ,16) near indexing
        nums2 = [int(x) for x in re.findall(r"\[(\d+)\]\|\d+,\s*16|\[(\d+)\].{0,12}16", t) if False]
    # fallbacks from old txid.json + common
    return []


def main() -> int:
    keys = json.loads(Path("/tmp/session_keys.json").read_text())
    sso = keys["sso"]
    sess = crequests.Session(impersonate="chrome146", proxies={"http": PROXY, "https": PROXY})
    sess.cookies.set("sso", sso, domain=".grok.com")
    sess.cookies.set("sso-rw", sso, domain=".grok.com")
    ip = sess.get("https://api.ipify.org?format=json", timeout=20).json()
    print(json.dumps({"phase": "egress", "ip": ip, "proxy": "70.39.164.200:30000"}, ensure_ascii=False), flush=True)

    # --- challenge ---
    e = os.urandom(32)
    priv = PrivateKey(e)
    pub = list(priv.public_key.format(compressed=True))
    html = sess.get("https://grok.com/c", headers={"User-Agent": UA, "Accept": "text/html"}, timeout=60).text
    baggage = (re.search(r'<meta name="baggage" content="([^"]+)"', html) or [None, ""])[1]
    sentry = (re.search(r'<meta name="sentry-trace" content="([^"-]+)', html) or [None, ""])[1]

    action_js = None
    action_url = ""
    for url in re.findall(r'src="(https://cdn\.grok\.com/_next/static/chunks/[^"]+\.js)"', html):
        t = sess.get(url, timeout=30).text
        if "anonPrivateKey" in t and "createServerReference" in t:
            action_js, action_url = t, url
            break
    refs = re.findall(r'createServerReference\)\("([a-f0-9]+)"', action_js or "")
    actions = refs[:3]
    print(json.dumps({"actions": actions, "action_url": action_url[-50:]}, ensure_ascii=False), flush=True)
    if len(actions) < 3:
        return 2

    def cheaders(action: str) -> dict:
        return {
            "sec-ch-ua-platform": '"Windows"',
            "next-action": action,
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
    r0 = sess.post("https://grok.com/c", headers=cheaders(actions[0]), multipart=mime, timeout=60)
    anon = re.search(r'\{"anonUserId":"([^"]+)"', r0.text or "")
    anon = anon.group(1) if anon else ""
    print(json.dumps({"r0": r0.status_code, "anon": bool(anon)}, ensure_ascii=False), flush=True)
    if not anon:
        return 3

    h1 = cheaders(actions[1])
    h1["content-type"] = "text/plain;charset=UTF-8"
    r1 = sess.post("https://grok.com/c", headers=h1, data=json.dumps([{"anonUserId": anon}]), timeout=60)
    hx = r1.content.hex()
    start = hx.find("3a6f38362c")
    if start < 0:
        print(json.dumps({"error": "no_challenge", "r1": r1.status_code}), flush=True)
        return 4
    start += len("3a6f38362c")
    end = hx.find("313a", start)
    challenge = bytes.fromhex(hx[start:end])
    sig = priv.sign_recoverable(hashlib.sha256(challenge).digest(), hasher=None)[:64]
    ch = {"challenge": base64.b64encode(challenge).decode(), "signature": base64.b64encode(sig).decode()}

    h2 = cheaders(actions[2])
    h2["content-type"] = "text/plain;charset=UTF-8"
    r2 = sess.post("https://grok.com/c", headers=h2, data=json.dumps([{"anonUserId": anon, **ch}]), timeout=60)
    text2 = r2.text or ""
    Path("/tmp/zb_r2_live.txt").write_text(text2, encoding="utf-8", errors="replace")
    ver_m = re.search(r'grok-site[^\"]*verification\",\"content\":\"([^\"]+)\"', text2)
    if not ver_m:
        print(json.dumps({"error": "no_ver", "r2": r2.status_code, "len": len(text2)}), flush=True)
        return 5
    ver = ver_m.group(1)
    meta = b64d(ver)
    anim_i = list(meta)[5] % 4

    # parse curves
    start = text2.find('"curves":')
    i = text2.find("[[", start)
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
    svg = curves_to_svg(curves[anim_i])
    xa_n = len(xa(svg))
    print(
        json.dumps(
            {
                "ver_prefix": ver[:24],
                "anim_i": anim_i,
                "groups": len(curves),
                "svg_len": len(svg),
                "xa_parts": xa_n,
                "svg_head": svg[:80],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    # discover / try x_values
    x_found = discover_x_values(sess)
    trials = []
    if x_found:
        trials.append(("discovered", x_found))
    for name, xv in [
        ("map_0_2_8_9", [0, 2, 8, 9]),
        ("map_14_10_25_24", [14, 10, 25, 24]),
        ("map_6_14_12_16", [6, 14, 12, 16]),
        ("map_11_24_38_38", [11, 24, 38, 38]),
        ("map_0_1_2_3", [0, 1, 2, 3]),
        ("map_5_6_7_8", [5, 6, 7, 8]),
        ("map_13_33_11_36", [13, 33, 11, 36]),
        ("map_25_10_30_26", [25, 10, 30, 26]),
        ("map_41_6_33_12", [41, 6, 33, 12]),
        ("map_31_26_18_35", [31, 26, 18, 35]),
        ("map_18_23_44_33", [18, 23, 44, 33]),
    ]:
        trials.append((name, xv))

    cookie = "; ".join(f"{k}={v}" for k, v in sess.cookies.get_dict().items())
    if "sso=" not in cookie:
        cookie = f"sso={sso}; sso-rw={sso}; " + cookie

    def chat(name: str, meta48: bytes, fp: str, *, image: bool = False) -> dict:
        sig = generate_statsig("POST", CHAT, meta48, fp)
        msg = "Drawing: a simple purple star, realistic, clear details" if image else "Reply with exactly: ZBOK"
        r = sess.post(
            "https://grok.com" + CHAT,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://grok.com",
                "Referer": "https://grok.com/",
                "User-Agent": UA,
                "Cookie": cookie,
                "x-statsig-id": sig,
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
        row = {"name": name, "http": r.status_code, "kind": kind, "fp_len": len(fp), "fp_prefix": fp[:28], "body": text[:100]}
        print(json.dumps(row, ensure_ascii=False), flush=True)
        return row

    # control: old keys still work?
    chat("ctrl_old_keys", b64d(keys["meta_b64"]), keys["fingerprint"])

    winner = None
    for name, xv in trials:
        try:
            fp = fingerprint(meta, svg, xv)
        except Exception as exc:
            print(json.dumps({"trial": name, "error": str(exc)[:100]}), flush=True)
            continue
        row = chat(f"zb_{name}", meta, fp)
        if row["kind"] == "chat_ok":
            winner = {"x_values": xv, "fp": fp, "trial": name}
            lite = chat("zb_lite", meta, fp, image=True)
            winner["lite"] = lite["kind"]
            break

    out = {
        "winner": winner,
        "anim_i": anim_i,
        "ver_prefix": ver[:24],
        "proxy": "70.39.164.200:30000",
        "zero_browser": True,
    }
    if winner:
        Path("/tmp/zb_zero_keys.json").write_text(
            json.dumps(
                {
                    "meta_b64": base64.b64encode(meta).decode(),
                    "fingerprint": winner["fp"],
                    "x_values": winner["x_values"],
                    "sso": sso,
                    "svg": svg[:200],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    Path("/tmp/zb_curves_svg_result.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps({"phase": "done", **out}, ensure_ascii=False), flush=True)
    return 0 if winner and winner.get("lite") == "image_ok" else 5


if __name__ == "__main__":
    raise SystemExit(main())
