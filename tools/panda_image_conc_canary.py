#!/usr/bin/env python3
"""Concurrent image canary: half long / half short prompts, pS off by default.

Short prompts: half with expand_prompt=true (pS), half explicit false.
Fetches pipeline segment timing from Admin image-timeline when admin password available.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("GROK2API_BASE", "http://127.0.0.1:18000").rstrip("/")
KEY = os.environ.get("GROK2API_KEY", "").strip()
MODEL = os.environ.get("GROK2API_IMAGE_MODEL", "grok-imagine-image")
TIMEOUT = float(os.environ.get("GROK2API_TIMEOUT", "180"))
CONCURRENCY = int(os.environ.get("GROK2API_CONCURRENCY", "4"))

LONG_PROMPTS = [
    (
        "zh_long_mist",
        "清晨薄雾中的江南水乡，青瓦白墙倒映在平静的河面上，岸边垂柳轻拂水面，远处石桥上有行人撑伞缓步而行，画面柔和而富有诗意",
    ),
    (
        "en_long_cafe",
        "A cozy corner cafe at dusk with warm pendant lights, wooden tables, steaming latte art, rain streaking the window, and a reader lost in an old paperback novel",
    ),
    (
        "zh_long_mountain",
        "秋日高原上的金色草甸延伸至远方，连绵雪山在夕阳下泛着淡紫光泽，几匹骏马悠闲吃草，天空中飘着大片棉絮状云朵",
    ),
    (
        "en_long_studio",
        "Minimalist product studio setup with softbox lighting, matte concrete backdrop, chrome tripod, and a designer chair casting a long dramatic shadow",
    ),
    (
        "zh_long_market",
        "热闹夜市摊位上悬挂着彩色灯笼，铁板烧烤冒出袅袅白烟，摊主熟练翻动着食材，顾客举着手机记录烟火气息",
    ),
    (
        "en_long_garden",
        "English cottage garden in late spring overflowing with peonies, lavender, and climbing roses along a weathered stone path leading to a blue door",
    ),
    (
        "zh_long_library",
        "古老图书馆高耸的拱顶下，胡桃木书架整齐排列，阳光透过彩绘玻璃洒在泛黄书页上，空气里弥漫着纸张与木蜡油的气息",
    ),
    (
        "en_long_coast",
        "Wind-swept coastal cliffs with turquoise waves crashing below, seabirds circling above rugged basalt columns under a dramatic stormy sky",
    ),
    (
        "zh_long_subway",
        "深夜地铁车厢里只有零星乘客，冷白灯光映在金属扶手上，窗外隧道壁飞速掠过形成流光轨迹，氛围安静而略带孤独",
    ),
    (
        "en_long_workshop",
        "Busy bicycle repair workshop with tools hanging on pegboards, grease-stained workbench, partially assembled frames, and a vintage radio playing softly",
    ),
]

SHORT_PROMPTS = [
    ("zh_short_iphone", "一台黑iPhone"),
    ("en_short_mug", "a red mug"),
    ("zh_short_cat", "一只橘猫"),
    ("en_short_cube", "blue cube on white"),
    ("zh_short_desk", "木质书桌台灯"),
    ("en_short_bottle", "glass water bottle"),
    ("zh_short_bike", "红色公路自行车"),
    ("en_short_chair", "oak dining chair"),
    ("zh_short_bag", "棕色皮革公文包"),
    ("en_short_plant", "monstera in clay pot"),
]


def load_admin_password() -> str | None:
    for path in (
        Path("/root/.secrets/grok2api-admin-password"),
        Path(os.environ.get("GROK2API_ADMIN_PASSWORD_FILE", "")),
    ):
        if path and path.exists():
            return path.read_text(encoding="utf-8").strip()
    return None


def admin_token() -> str | None:
    password = load_admin_password()
    if not password:
        return None
    body = json.dumps({"username": "admin", "password": password}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/admin/v1/auth/login",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
        return payload.get("data", payload)["tokens"]["accessToken"]


def fetch_timeline(token: str, window: str = "10m") -> list[dict]:
    req = urllib.request.Request(
        f"{BASE}/api/admin/v1/image-timeline?window={window}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode())
        return payload.get("data", payload).get("traces", [])


def build_cases(n: int) -> list[dict]:
    long_n = n // 2
    short_n = n - long_n
    short_ps_n = short_n // 2
    cases: list[dict] = []
    for i in range(long_n):
        name, prompt = LONG_PROMPTS[i % len(LONG_PROMPTS)]
        if i >= len(LONG_PROMPTS):
            name = f"{name}_{i}"
        cases.append(
            {
                "name": name,
                "prompt": prompt,
                "category": "long",
                "expand_prompt": False,
                "expect_ps": False,
                "prompt_chars": len(prompt),
            }
        )
    for i in range(short_ps_n):
        name, prompt = SHORT_PROMPTS[i % len(SHORT_PROMPTS)]
        if i >= len(SHORT_PROMPTS):
            name = f"{name}_ps_{i}"
        cases.append(
            {
                "name": name,
                "prompt": prompt,
                "category": "short",
                "expand_prompt": True,
                "expect_ps": True,
                "prompt_chars": len(prompt),
            }
        )
    for i in range(short_n - short_ps_n):
        idx = short_ps_n + i
        name, prompt = SHORT_PROMPTS[idx % len(SHORT_PROMPTS)]
        if idx >= len(SHORT_PROMPTS):
            name = f"{name}_nop_{i}"
        cases.append(
            {
                "name": name,
                "prompt": prompt,
                "category": "short",
                "expand_prompt": False,
                "expect_ps": False,
                "prompt_chars": len(prompt),
            }
        )
    return cases


def one(case: dict) -> dict:
    started = time.perf_counter()
    payload = {
        "model": MODEL,
        "prompt": case["prompt"],
        "n": 1,
        "expand_prompt": case["expand_prompt"],
    }
    body = json.dumps(payload).encode()
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
            item_payload = json.loads(raw.decode("utf-8", errors="replace"))
            data = item_payload.get("data") or []
            item = data[0] if data else {}
            request_id = resp.headers.get("X-Request-ID", "")
            revised = item.get("revised_prompt") or ""
            used_ps = bool(revised.strip()) and revised.strip() != case["prompt"].strip()
            return {
                **case,
                "ok": resp.status == 200 and bool(item.get("url") or item.get("b64_json")),
                "status": resp.status,
                "seconds": round(time.perf_counter() - started, 3),
                "request_id": request_id,
                "url": item.get("url"),
                "revised_prompt": revised,
                "used_ps": used_ps,
                "error": "",
            }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return {
            **case,
            "ok": False,
            "status": exc.code,
            "seconds": round(time.perf_counter() - started, 3),
            "request_id": exc.headers.get("X-Request-ID", ""),
            "url": "",
            "revised_prompt": "",
            "used_ps": False,
            "error": detail,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            **case,
            "ok": False,
            "status": 0,
            "seconds": round(time.perf_counter() - started, 3),
            "request_id": "",
            "url": "",
            "revised_prompt": "",
            "used_ps": False,
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


def enrich_pipeline(rows: list[dict], token: str | None) -> None:
    if not token:
        return
    try:
        traces = fetch_timeline(token)
    except Exception as exc:  # noqa: BLE001
        for row in rows:
            row["pipeline_error"] = str(exc)
        return
    by_request = {t.get("requestId"): t for t in traces if t.get("requestId")}
    for row in rows:
        rid = row.get("request_id")
        trace = by_request.get(rid)
        if not trace:
            continue
        row["pipeline"] = {
            "total_ms": trace.get("totalMs"),
            "queue_ms": trace.get("queueMs"),
            "ps_queue_ms": trace.get("psQueueMs"),
            "ss_queue_ms": trace.get("ssQueueMs"),
            "download_queue_ms": trace.get("downloadQueueMs"),
            "expand_ms": trace.get("expandMs"),
            "sse_ms": trace.get("sseMs"),
            "download_ms": trace.get("downloadMs"),
            "status": trace.get("status"),
            "error_code": trace.get("errorCode"),
        }


def run_batch(concurrency: int, token: str | None) -> dict:
    cases = build_cases(concurrency)
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(one, case) for case in cases]
        rows = [f.result() for f in futures]
    elapsed = round(time.perf_counter() - started, 3)
    enrich_pipeline(rows, token)
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
        "mix": {
            "long": sum(1 for r in rows if r["category"] == "long"),
            "short_ps": sum(1 for r in rows if r["category"] == "short" and r["expand_prompt"]),
            "short_no_ps": sum(1 for r in rows if r["category"] == "short" and not r["expand_prompt"]),
        },
        "results": rows,
    }


def main() -> int:
    if not KEY:
        print("GROK2API_KEY required", file=sys.stderr)
        return 2
    token = admin_token()
    groups_raw = os.environ.get("GROK2API_GROUPS", "").strip()
    if groups_raw:
        groups = [int(part.strip()) for part in groups_raw.split(",") if part.strip()]
    else:
        groups = [CONCURRENCY]
    batches = [run_batch(n, token) for n in groups]
    report = {
        "base": BASE,
        "model": MODEL,
        "expand_prompt_default": False,
        "batches": batches,
        "all_passed": all(batch["all_passed"] for batch in batches),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
