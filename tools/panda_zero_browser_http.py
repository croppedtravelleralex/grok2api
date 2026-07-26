#!/usr/bin/env python3
"""Zero-browser grok chat+Lite on panda: page verification + SVG fp OR full anon challenge.

Modes:
  --mode page   SSO + GET / → extract verification + SVG + x_values → mint statsig → chat/Lite
  --mode anon   Grok-Api style 3-step /c challenge (optional SSO cookie merge) then chat/Lite
  --mode reuse  Use existing session_keys.json meta+fp (control)

Proxies:
  --proxy-url explicit
  --proxy-pool la|webshare|udeal|scan
"""
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
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from curl_cffi import requests as crequests

EPOCH = 1682924400
CHAT = "/rest/app-chat/conversations/new"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
SEC_CH = '"Google Chrome";v="146", "Chromium";v="146", "Not A(Brand";v="24"'


def b64d(s: str) -> bytes:
    return base64.b64decode(s + "=" * ((4 - len(s) % 4) % 4))


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode().rstrip("=")


# ---- XCTID fingerprint (from Grok-Api xctid.py) ----
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

    def z(v):
        return abs(v) < 1e-7

    def integ(v):
        return abs(v - round(v)) < 1e-7

    cosv, sinv = math.cos(rad), math.sin(rad)
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


def fingerprint_from_svg(verification_bytes: bytes, svg: str, x_values: list) -> str:
    arr = list(verification_bytes)
    idx = arr[x_values[0]] % 16
    c = ((arr[x_values[1]] % 16) * (arr[x_values[2]] % 16)) * (arr[x_values[3]] % 16)
    vals = xa(svg)[idx]
    k = simulate_style(vals, c)
    concat = str(k["color"]) + str(k["transform"])
    matches = re.findall(r"[\d.\-]+", concat)
    return "".join(tohex(float(m)) for m in matches).replace(".", "").replace("-", "")


def generate_statsig(method: str, path: str, meta48: bytes, fingerprint: str) -> str:
    n = int(time.time() - EPOCH)
    dig = f"{method}!{path}!{n}obfiowerehiring{fingerprint}"
    sha = hashlib.sha256(dig.encode()).digest()[:16]
    key = random.randint(0, 255)
    block = meta48 + struct.pack("<I", n) + sha + b"\x03"
    enc = bytearray([key]) + bytearray(b ^ key for b in block)
    return b64e(bytes(enc))


def between(text: str, a: str, b: str) -> str:
    i = text.find(a)
    if i < 0:
        return ""
    i += len(a)
    j = text.find(b, i)
    return text[i:j] if j >= 0 else ""


def extract_verification(html: str) -> str | None:
    for pat in (
        '"name":"grok-site-verification","content":"',
        '"name":"grok-site―verification","content":"',
        'name="grok-site-verification" content="',
        'name="grok-site―verification" content="',
    ):
        v = between(html, pat, '"')
        if v and len(v) > 40:
            return v
    m = re.search(
        r'name=["\']grok-site[^"\']*verification["\'][^>]*content=["\']([^"\']+)',
        html,
        re.I,
    )
    return m.group(1) if m else None


def extract_svg_paths(html: str) -> list[str]:
    return re.findall(r'"d":"(M[^"]{200,})"', html)


def discover_x_values(sess, html: str, script_hint: str = "") -> tuple[list[int], str]:
    """Find x[N],16 indices from xsid chunk; cache by URL."""
    candidates = []
    if script_hint:
        candidates.append(script_hint if script_hint.startswith("http") else f"https://grok.com/_next/{script_hint}")
    # any chunk urls in page
    for rel in re.findall(r'/_next/static/chunks/[^"\']+\.js', html)[:120]:
        candidates.append("https://grok.com" + rel)
    # also cdn
    for rel in re.findall(r'https://cdn\.grok\.com/_next/static/chunks/[^"\']+\.js', html)[:40]:
        candidates.append(rel)

    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            t = sess.get(url, timeout=25).text
        except Exception:
            continue
        nums = [int(x) for x in re.findall(r"x\[(\d+)\]\s*,\s*16", t)]
        if len(nums) >= 4:
            return nums[:4], url
    return [], ""


def parse_actions_and_xsid(sess, scripts: list[str]) -> tuple[list[str], str]:
    """Port of Parser.parse_grok."""
    mapping_path = Path("/tmp/grok-api-ref/core/mappings/grok.json")
    if mapping_path.exists():
        for index in json.loads(mapping_path.read_text()):
            if index.get("action_script") in scripts:
                return index["actions"], index["xsid_script"]

    action_script = xsid_script = None
    script_content1 = script_content2 = ""
    for script in scripts:
        try:
            content = sess.get(f"https://grok.com{script}", timeout=30).text
        except Exception:
            continue
        if "anonPrivateKey" in content:
            script_content1 = content
            action_script = script
        if "880932)" in content:
            script_content2 = content
    actions = re.findall(r'createServerReference\)\("([a-f0-9]+)"', script_content1) if script_content1 else []
    m = re.search(r'"(static/chunks/[^"]+\.js)"[^}]*?\(880932\)', script_content2) if script_content2 else None
    xsid_script = m.group(1) if m else ""
    return actions, xsid_script


# ---- proxies ----
def la_proxy() -> str:
    """从环境变量读取出口代理。禁止硬编码凭据——本仓库是公开 fork。

    例：GROK_EGRESS_PROXY="http://user:pass@host:port"
    """
    proxy = os.environ.get("GROK_EGRESS_PROXY", "").strip()
    if not proxy:
        raise SystemExit("需要设置 GROK_EGRESS_PROXY")
    return proxy


def webshare_proxies(path: str = "/tmp/webshare100.txt") -> list[str]:
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


def udeal_proxies(path: str = "/tmp/udeal1000proxy.txt", limit: int = 40) -> list[str]:
    out = []
    for ln in Path(path).read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        if "session=" not in ln:
            continue
        s = (parse_qs(urlsplit(ln.strip()).query).get("session") or [""])[0].strip()
        if not s:
            continue
        user = f"userId-2684-custom-8241-region-sg-session-{s}-sessTime-15"
        out.append(f"http://{quote(user)}:{quote('IM0aSd')}@as.udealproxy.com:6666")
        if len(out) >= limit:
            break
    return list(dict.fromkeys(out))


def probe_home(proxy: str) -> dict:
    try:
        r = crequests.get(
            "https://grok.com/",
            proxies={"http": proxy, "https": proxy},
            impersonate="chrome146",
            timeout=25,
            headers={"User-Agent": UA},
        )
        text = r.text or ""
        ok = r.status_code == 200 and "<title>Grok</title>" in text and "Just a moment" not in text
        return {
            "proxy": proxy.split("@")[-1] if "@" in proxy else proxy,
            "http": r.status_code,
            "ok": ok,
            "cf": r.headers.get("cf-mitigated"),
            "bytes": len(text),
        }
    except Exception as exc:
        return {"proxy": proxy.split("@")[-1] if "@" in proxy else proxy, "ok": False, "error": f"{type(exc).__name__}:{str(exc)[:100]}"}


def chat_payload(msg: str, *, image: bool = False) -> dict:
    return {
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
    }


def classify(st: int, body: str, headers: dict) -> str:
    low = (body or "").lower()
    if headers.get("cf-mitigated") == "challenge" or "just a moment" in low:
        return "cf_challenge"
    if st == 403 and "anti-bot" in low:
        return "anti_bot"
    if st == 200 and ("assets.grok" in body or "generatedImageUrls" in body or "generated" in low):
        return "image_ok"
    if st == 200 and ("conversation" in low or "token" in low):
        return "chat_ok"
    return f"http_{st}"


def do_chat(sess, cookie: str, meta: bytes, fp: str, name: str, *, image: bool = False) -> dict:
    sig = generate_statsig("POST", CHAT, meta, fp)
    msg = (
        "Drawing: a simple green triangle, realistic, clear details"
        if image
        else "Reply with exactly: ZBOK"
    )
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://grok.com",
        "Referer": "https://grok.com/",
        "User-Agent": UA,
        "sec-ch-ua": SEC_CH,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Cookie": cookie,
        "x-statsig-id": sig,
        "x-xai-request-id": str(uuid.uuid4()),
    }
    t0 = time.time()
    r = sess.post("https://grok.com" + CHAT, headers=headers, json=chat_payload(msg, image=image), timeout=120)
    text = r.text or ""
    row = {
        "name": name,
        "http": r.status_code,
        "kind": classify(r.status_code, text, dict(r.headers)),
        "elapsed_s": round(time.time() - t0, 2),
        "fp_len": len(fp),
        "meta_len": len(meta),
        "body": text[:140].replace("\n", " "),
    }
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return row


def mode_page(sess, sso: str) -> dict:
    """SSO + homepage verification + SVG fingerprint — no Chrome."""
    out: dict = {"mode": "page"}
    sess.cookies.set("sso", sso, domain=".grok.com")
    sess.cookies.set("sso-rw", sso, domain=".grok.com")
    home = sess.get("https://grok.com/", headers={"User-Agent": UA, "Accept": "text/html"}, timeout=60)
    html = home.text or ""
    out["home_http"] = home.status_code
    out["home_cf"] = home.headers.get("cf-mitigated")
    out["title_ok"] = "<title>Grok</title>" in html
    if "Just a moment" in html or home.headers.get("cf-mitigated") == "challenge":
        out["error"] = "cf_challenge"
        return out

    ver = extract_verification(html)
    if not ver:
        out["error"] = "no_verification"
        Path("/tmp/zb_home_snip.html").write_text(html[:30000], encoding="utf-8", errors="replace")
        return out
    meta = b64d(ver)
    out["ver_bytes"] = len(meta)
    anim_idx = meta[5] % 4 if len(meta) > 5 else 0
    paths = extract_svg_paths(html)
    out["svg_paths"] = len(paths)
    if not paths:
        # try /c page which often embeds anim
        cpage = sess.get("https://grok.com/c", headers={"User-Agent": UA, "Accept": "text/html"}, timeout=60)
        html2 = cpage.text or ""
        paths = extract_svg_paths(html2)
        out["svg_paths_c"] = len(paths)
        if paths:
            html = html2
        ver2 = extract_verification(html2)
        if ver2:
            ver = ver2
            meta = b64d(ver)
            anim_idx = meta[5] % 4
            out["ver_from"] = "c_page"

    if not paths:
        out["error"] = "no_svg"
        Path("/tmp/zb_home_snip.html").write_text(html[:30000], encoding="utf-8", errors="replace")
        return out

    svg = paths[anim_idx] if anim_idx < len(paths) else paths[0]
    x_values, xsid_url = discover_x_values(sess, html)
    out["x_values"] = x_values
    out["xsid_url_tail"] = xsid_url[-60:] if xsid_url else ""
    if len(x_values) < 4:
        # fallback common mappings
        for trial in ([0, 2, 8, 9], [14, 10, 25, 24], [6, 14, 12, 16], [11, 24, 38, 38]):
            try:
                fp = fingerprint_from_svg(meta, svg, trial)
                out["x_values"] = trial
                out["fp"] = fp
                out["fp_source"] = "fallback_map"
                break
            except Exception as exc:
                out["fallback_err"] = str(exc)[:80]
        else:
            out["error"] = "no_x_values"
            return out
    else:
        fp = fingerprint_from_svg(meta, svg, x_values)
        out["fp"] = fp
        out["fp_source"] = "discovered"

    jar = "; ".join(f"{k}={v}" for k, v in sess.cookies.get_dict().items())
    cookie = jar if "sso=" in jar else f"sso={sso}; sso-rw={sso}" + (f"; {jar}" if jar else "")
    out["cookie_names"] = sorted(sess.cookies.get_dict().keys())
    out["keys"] = {"meta_b64": b64e(meta), "fingerprint": out["fp"]}
    return {**out, "cookie": cookie, "meta": meta, "fp": out["fp"]}


def mode_anon(sess, sso: str | None) -> dict:
    """Full Grok-Api /c challenge chain."""
    from coincurve import PrivateKey
    from curl_cffi import CurlMime

    out: dict = {"mode": "anon"}
    if sso:
        sess.cookies.set("sso", sso, domain=".grok.com")
        sess.cookies.set("sso-rw", sso, domain=".grok.com")

    # generate keys
    e = os.urandom(32)
    priv = PrivateKey(e)
    pub = list(priv.public_key.format(compressed=True))
    # Grok-Api Anon.xor(e) == b64(raw 32-byte private key)
    private_key_b64 = base64.b64encode(e).decode()

    load_headers = {
        "upgrade-insecure-requests": "1",
        "user-agent": UA,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "sec-ch-ua": SEC_CH,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-site": "none",
        "sec-fetch-mode": "navigate",
        "sec-fetch-dest": "document",
    }
    load_site = sess.get("https://grok.com/c", headers=load_headers, timeout=60)
    html = load_site.text or ""
    out["load_http"] = load_site.status_code
    out["load_cf"] = load_site.headers.get("cf-mitigated")
    if "Just a moment" in html or load_site.status_code != 200:
        out["error"] = "cf_or_load_fail"
        out["body_head"] = html[:120]
        return out

    scripts = re.findall(r'src="(/_next/static/chunks/[^"]+)"', html)
    actions, xsid_script = parse_actions_and_xsid(sess, scripts)
    out["actions_n"] = len(actions)
    out["xsid_script"] = xsid_script
    if len(actions) < 3:
        out["error"] = "no_actions"
        Path("/tmp/zb_c_snip.html").write_text(html[:40000], encoding="utf-8", errors="replace")
        return out

    baggage = between(html, '<meta name="baggage" content="', '"')
    sentry_trace = between(html, '<meta name="sentry-trace" content="', "-")
    out["has_baggage"] = bool(baggage)

    def c_headers(next_action: str) -> dict:
        return {
            "sec-ch-ua-platform": '"Windows"',
            "next-action": next_action,
            "sec-ch-ua": SEC_CH,
            "sec-ch-ua-mobile": "?0",
            "next-router-state-tree": "%5B%22%22%2C%7B%22children%22%3A%5B%22c%22%2C%7B%22children%22%3A%5B%5B%22slug%22%2C%22%22%2C%22oc%22%5D%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D",
            "baggage": baggage,
            "sentry-trace": f"{sentry_trace}-{uuid.uuid4().hex[:16]}-0",
            "user-agent": UA,
            "accept": "text/x-component",
            "origin": "https://grok.com",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": "https://grok.com/c",
        }

    # run 0 multipart
    mime = CurlMime()
    mime.addpart(name="1", data=bytes(pub), filename="blob", content_type="application/octet-stream")
    mime.addpart(name="0", filename=None, data='[{"userPublicKey":"$o1"}]')
    h0 = c_headers(actions[0])
    r0 = sess.post("https://grok.com/c", headers=h0, multipart=mime, timeout=60)
    anon_user = between(r0.text, '{"anonUserId":"', '"')
    out["r0_http"] = r0.status_code
    out["anon_user"] = bool(anon_user)
    if not anon_user:
        out["error"] = "no_anon"
        out["r0_head"] = (r0.text or "")[:200]
        return out

    # run 1
    h1 = c_headers(actions[1])
    h1["content-type"] = "text/plain;charset=UTF-8"
    r1 = sess.post("https://grok.com/c", headers=h1, data=json.dumps([{"anonUserId": anon_user}]), timeout=60)
    out["r1_http"] = r1.status_code
    hx = r1.content.hex()
    start_idx = hx.find("3a6f38362c")
    if start_idx < 0:
        out["error"] = "no_challenge"
        out["r1_head"] = (r1.text or "")[:200]
        return out
    start_idx += len("3a6f38362c")
    end_idx = hx.find("313a", start_idx)
    challenge_bytes = bytes.fromhex(hx[start_idx:end_idx])
    sig = priv.sign_recoverable(hashlib.sha256(challenge_bytes).digest(), hasher=None)[:64]
    challenge_dict = {
        "challenge": base64.b64encode(challenge_bytes).decode(),
        "signature": base64.b64encode(sig).decode(),
    }
    out["challenge_len"] = len(challenge_bytes)

    # run 2
    h2 = c_headers(actions[2])
    h2["content-type"] = "text/plain;charset=UTF-8"
    r2 = sess.post(
        "https://grok.com/c",
        headers=h2,
        data=json.dumps([{"anonUserId": anon_user, **challenge_dict}]),
        timeout=60,
    )
    out["r2_http"] = r2.status_code
    text2 = r2.text or ""
    ver = extract_verification(text2)
    if not ver:
        # try get_anim style
        ver = between(text2, '"name":"grok-site-verification","content":"', '"')
    if not ver:
        out["error"] = "no_ver_after_challenge"
        Path("/tmp/zb_r2.txt").write_text(text2[:50000], encoding="utf-8", errors="replace")
        return out
    meta = b64d(ver)
    anim = "loading-x-anim-" + str(list(meta)[5] % 4)
    paths = extract_svg_paths(text2)
    out["svg_paths"] = len(paths)
    if not paths:
        out["error"] = "no_svg_after_challenge"
        return out
    anim_i = int(anim.split("-")[-1])
    svg = paths[anim_i] if anim_i < len(paths) else paths[0]
    x_values, xsid_url = discover_x_values(sess, text2, script_hint=xsid_script)
    if len(x_values) < 4:
        # try mapping file
        txid = Path("/tmp/grok-api-ref/core/mappings/txid.json")
        if txid.exists() and xsid_script:
            mp = json.loads(txid.read_text())
            key = f"https://grok.com/_next/{xsid_script}"
            x_values = mp.get(key) or []
        if len(x_values) < 4:
            x_values = [0, 2, 8, 9]
            out["x_fallback"] = True
    fp = fingerprint_from_svg(meta, svg, x_values)
    out["fp"] = fp
    out["x_values"] = x_values
    out["ver_bytes"] = len(meta)

    jar = "; ".join(f"{k}={v}" for k, v in sess.cookies.get_dict().items())
    if sso and "sso=" not in jar:
        jar = f"sso={sso}; sso-rw={sso}; " + jar
    return {**out, "cookie": jar, "meta": meta, "fp": fp, "keys": {"meta_b64": b64e(meta), "fingerprint": fp}}


def main() -> int:
    os.environ.pop("SSL_CERT_FILE", None)
    os.environ.pop("REQUESTS_CA_BUNDLE", None)
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["page", "anon", "reuse", "scan"], default="page")
    ap.add_argument("--pool", choices=["la", "webshare", "udeal", "explicit"], default="la")
    ap.add_argument("--proxy-url", default="")
    ap.add_argument("--scan-n", type=int, default=20)
    ap.add_argument("--sso-json", default="/tmp/web-sso-canary.json")
    ap.add_argument("--keys", default="/tmp/session_keys.json")
    ap.add_argument("--account-id", type=int, default=209)
    ap.add_argument("--out", default="/tmp/zb_panda_result.json")
    args = ap.parse_args()

    # SSO
    sso = None
    if Path(args.keys).exists():
        keys = json.loads(Path(args.keys).read_text())
        sso = keys.get("sso")
    if Path(args.sso_json).exists():
        accs = json.loads(Path(args.sso_json).read_text()).get("accounts") or []
        hit = next((a for a in accs if a.get("id") == args.account_id), None)
        if hit:
            s = hit.get("sso") or ""
            sso = s[4:] if s.lower().startswith("sso=") else s

    if args.mode == "scan":
        pools = []
        if args.pool in ("webshare",):
            pools = webshare_proxies()[: args.scan_n]
        elif args.pool == "udeal":
            pools = udeal_proxies(limit=args.scan_n)
        elif args.pool == "la":
            pools = [la_proxy()]
        results = []
        for p in pools:
            row = probe_home(p)
            print(json.dumps({"phase": "probe", **row}, ensure_ascii=False), flush=True)
            results.append(row)
        ok = [r for r in results if r.get("ok")]
        Path(args.out).write_text(json.dumps({"ok_n": len(ok), "results": results}, indent=2), encoding="utf-8")
        print(json.dumps({"phase": "scan_done", "ok_n": len(ok), "out": args.out}), flush=True)
        return 0 if ok else 3

    # pick proxy
    proxy = args.proxy_url
    if not proxy:
        if args.pool == "la":
            proxy = la_proxy()
        elif args.pool == "webshare":
            # probe until ok
            for p in webshare_proxies()[: args.scan_n]:
                row = probe_home(p)
                print(json.dumps({"phase": "probe", **row}, ensure_ascii=False), flush=True)
                if row.get("ok"):
                    proxy = p
                    break
        elif args.pool == "udeal":
            for p in udeal_proxies(limit=args.scan_n):
                row = probe_home(p)
                print(json.dumps({"phase": "probe", **row}, ensure_ascii=False), flush=True)
                if row.get("ok"):
                    proxy = p
                    break
    if not proxy:
        print(json.dumps({"error": "no_proxy"}), flush=True)
        return 2

    sess = crequests.Session(impersonate="chrome146", proxies={"http": proxy, "https": proxy})
    try:
        ip = sess.get("https://api.ipify.org?format=json", timeout=20).json()
    except Exception as exc:
        ip = {"error": str(exc)[:100]}
    print(json.dumps({"phase": "egress", "ip": ip, "pool": args.pool}, ensure_ascii=False), flush=True)

    rows = []
    boot: dict = {}
    if args.mode == "reuse":
        keys = json.loads(Path(args.keys).read_text())
        meta = b64d(keys["meta_b64"])
        fp = keys["fingerprint"]
        cookie = f"sso={keys['sso']}; sso-rw={keys['sso']}"
        boot = {"mode": "reuse", "fp": fp, "meta_len": len(meta)}
    elif args.mode == "page":
        boot = mode_page(sess, sso or "")
        print(json.dumps({k: v for k, v in boot.items() if k not in ("cookie", "meta", "fp")}, ensure_ascii=False), flush=True)
        if boot.get("error"):
            Path(args.out).write_text(json.dumps({"boot": boot, "ip": ip}, indent=2, default=str), encoding="utf-8")
            return 4
        meta, fp, cookie = boot["meta"], boot["fp"], boot["cookie"]
    else:
        boot = mode_anon(sess, sso)
        print(json.dumps({k: v for k, v in boot.items() if k not in ("cookie", "meta", "fp", "keys")}, ensure_ascii=False), flush=True)
        if boot.get("error"):
            Path(args.out).write_text(json.dumps({"boot": boot, "ip": ip}, indent=2, default=str), encoding="utf-8")
            return 4
        meta, fp, cookie = boot["meta"], boot["fp"], boot["cookie"]

    rows.append(do_chat(sess, cookie, meta, fp, f"{args.mode}_text", image=False))
    rows.append(do_chat(sess, cookie, meta, fp, f"{args.mode}_lite", image=True))
    verdict = {
        "text_ok": any(r["kind"] == "chat_ok" for r in rows if "text" in r["name"]),
        "image_ok": any(r["kind"] == "image_ok" for r in rows if "lite" in r["name"]),
        "mode": args.mode,
        "pool": args.pool,
        "zero_browser": args.mode != "reuse",
    }
    verdict["pass"] = verdict["text_ok"] and verdict["image_ok"]
    out = {"verdict": verdict, "rows": rows, "boot": {k: v for k, v in boot.items() if k not in ("cookie", "meta")}, "ip": ip}
    if "keys" in boot:
        out["keys"] = boot["keys"]
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"phase": "done", "verdict": verdict, "out": args.out}, ensure_ascii=False), flush=True)
    return 0 if verdict["pass"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
