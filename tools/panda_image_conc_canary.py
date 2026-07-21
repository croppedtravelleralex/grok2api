#!/usr/bin/env python3
"""Concurrent short zh/en Lite image canary for pipeline tuning (2/4/10)."""

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
TIMEOUT = float(os.environ.get("GROK2API_TIMEOUT", "180"))
CONCURRENCY = int(os.environ.get("GROK2API_CONCURRENCY", "4"))

BASE_PROMPTS = [
    ("zh_iphone", "一台黑iPhone"),
    ("en_mug", "a red mug"),
    ("zh_cat", "一只橘猫"),
    ("en_cube", "blue cube on white"),
    ("zh_desk", "木质书桌台灯"),
    ("en_bottle", "glass water bottle"),
    ("zh_bike", "红色公路自行车"),
    ("en_chair", "oak dining chair"),
    ("zh_bag", "棕色皮革公文包"),
    ("en_plant", "monstera in clay pot"),
]


def build_prompts(n: int) -> list[tuple[str, str]]:
    if n <= len(BASE_PROMPTS):
        return BASE_PROMPTS[:n]
    out: list[tuple[str, str]] = []
    while len(out) < n:
        for name, prompt in BASE_PROMPTS:
            out.append((f"{name}_{len(out)}", prompt))
            if len(out) >= n:
                break
    return out


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


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return round(ordered[low] * (1 - weight) + ordered[high] * weight, 3)


def run_batch(concurrency: int) -> dict:
    prompts = build_prompts(concurrency)
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(one, name, prompt) for name, prompt in prompts]
        rows = [f.result() for f in futures]
    elapsed = round(time.perf_counter() - started, 3)
    oks = [r for r in rows if r["ok"]]
    secs = [r["seconds"] for r in rows]
    return {
        "concurrency": concurrency,
        "wall_seconds": elapsed,
        "success": len(oks),
        "total": len(rows),
        "all_passed": len(oks) == len(rows),
        "latency_p50": percentile(secs, 0.5),
        "latency_p90": percentile(secs, 0.9),
        "latency_max": max(secs) if secs else None,
        "results": rows,
    }


def main() -> int:
    if not KEY:
        print("GROK2API_KEY required", file=sys.stderr)
        return 2
    groups_raw = os.environ.get("GROK2API_GROUPS", "").strip()
    if groups_raw:
        groups = [int(part.strip()) for part in groups_raw.split(",") if part.strip()]
    else:
        groups = [CONCURRENCY]
    batches = [run_batch(n) for n in groups]
    report = {
        "base": BASE,
        "model": MODEL,
        "batches": batches,
        "all_passed": all(batch["all_passed"] for batch in batches),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
