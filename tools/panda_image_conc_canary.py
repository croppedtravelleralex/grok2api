#!/usr/bin/env python3
"""4-way concurrent short zh/en Lite image canary against local grok2api."""

from __future__ import annotations

import concurrent.futures
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("GROK2API_BASE", "http://127.0.0.1:18000").rstrip("/")
KEY = os.environ.get("GROK2API_KEY", "").strip()
MODEL = os.environ.get("GROK2API_IMAGE_MODEL", "grok-imagine-image")
TIMEOUT = float(os.environ.get("GROK2API_TIMEOUT", "120"))

PROMPTS = [
    ("zh_iphone", "一台黑iPhone"),
    ("en_mug", "a red mug"),
    ("zh_cat", "一只橘猫"),
    ("en_cube", "blue cube on white"),
]


def one(name: str, prompt: str) -> dict:
    started = time.perf_counter()
    body = json.dumps({"model": MODEL, "prompt": prompt, "n": 1}).encode()
    req = urllib.request.Request(
        f"{BASE}/v1/images/generations",
        data=body,
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            data = payload.get("data") or []
            item = data[0] if data else {}
            return {
                "name": name,
                "prompt": prompt,
                "ok": resp.status == 200 and bool(item.get("url") or item.get("b64_json")),
                "status": resp.status,
                "seconds": round(time.perf_counter() - started, 3),
                "url": item.get("url"),
                "revised_prompt": item.get("revised_prompt") or "",
                "error": "",
            }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return {
            "name": name,
            "prompt": prompt,
            "ok": False,
            "status": exc.code,
            "seconds": round(time.perf_counter() - started, 3),
            "url": "",
            "revised_prompt": "",
            "error": detail,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": name,
            "prompt": prompt,
            "ok": False,
            "status": 0,
            "seconds": round(time.perf_counter() - started, 3),
            "url": "",
            "revised_prompt": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> int:
    if not KEY:
        print("GROK2API_KEY required", file=sys.stderr)
        return 2
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(one, name, prompt) for name, prompt in PROMPTS]
        rows = [f.result() for f in futures]
    elapsed = round(time.perf_counter() - started, 3)
    oks = [r for r in rows if r["ok"]]
    secs = [r["seconds"] for r in rows]
    report = {
        "base": BASE,
        "model": MODEL,
        "concurrency": 4,
        "wall_seconds": elapsed,
        "success": len(oks),
        "total": len(rows),
        "all_passed": len(oks) == len(rows),
        "latency_p50": round(statistics.median(secs), 3) if secs else None,
        "latency_max": max(secs) if secs else None,
        "results": rows,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
