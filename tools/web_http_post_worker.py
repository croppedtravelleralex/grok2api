#!/usr/bin/env python3
"""One-shot curl_cffi POST worker for the Rust canary (stdin JSON → stdout JSON)."""
from __future__ import annotations

import json
import re
import sys
import time
import uuid

from curl_cffi import requests as crequests

PROXY = "http://127.0.0.1:7897"
PROXIES = {"http": PROXY, "https": PROXY}
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
CHAT_URL = "https://grok.com/rest/app-chat/conversations/new"
IMPERSONATE = "chrome131"


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


def classify(status: int, body: str, cf: str | None, want_image: bool) -> str:
    lower = body.lower()
    if cf or "just a moment" in lower:
        return "cf_challenge"
    if status == 403 and "anti-bot" in lower:
        return "anti_bot_403"
    if status == 401:
        return "auth_401"
    images = extract_image_urls(body)
    if want_image and status == 200 and images:
        return "image_ok"
    if status == 200 and (("PONG" in body) or ("token" in lower) or ('"message"' in body)):
        return "chat_ok"
    if status == 200:
        return "http_200_unclear"
    return f"http_{status}"


def main() -> None:
    req = json.load(sys.stdin)
    cookie = req["cookie"]
    statsig = req.get("statsig")
    payload = req["payload"]
    want_image = bool(req.get("want_image"))
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
        "user-agent": UA,
        "cookie": cookie,
        "cache-control": "no-cache",
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
    for chunk in r.iter_content(chunk_size=8192):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total >= 2_000_000:
            break
    body = b"".join(chunks).decode("utf-8", "replace")
    images = extract_image_urls(body)
    kind = classify(r.status_code, body, r.headers.get("cf-mitigated"), want_image)
    out = {
        "http": r.status_code,
        "cf": r.headers.get("cf-mitigated"),
        "elapsed_s": round(time.time() - t0, 2),
        "has_statsig": bool(statsig),
        "statsig_len": len(statsig) if statsig else 0,
        "kind": kind,
        "has_token_hint": ("token" in body.lower()) or ("PONG" in body),
        "image_count": len(images),
        "image_urls_prefix": [u[:120] for u in images[:3]],
        "body_prefix": body[:320].replace("\n", " "),
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
