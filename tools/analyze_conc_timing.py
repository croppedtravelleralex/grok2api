#!/usr/bin/env python3
"""从 Admin image-timeline 聚合生图各阶段耗时（用于 10/20 并发验收分析）。"""

from __future__ import annotations

import json
import os
import statistics
import urllib.request
from pathlib import Path


BASE = os.environ.get("GROK2API_BASE", "http://127.0.0.1:18000").rstrip("/")
WINDOW = os.environ.get("GROK2API_TIMING_WINDOW", "30m")


def load_password() -> str:
    path = Path("/root/.secrets/grok2api-admin-password")
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    raise SystemExit("admin password missing")


def api(method: str, path: str, token: str | None = None, body: dict | None = None) -> dict:
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = json.loads(resp.read().decode())
        return payload.get("data", payload)


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    w = rank - low
    return round(ordered[low] * (1 - w) + ordered[high] * w, 1)


def summarize(traces: list[dict]) -> dict:
    ok = [t for t in traces if t.get("status") == "succeeded"]
    fail = [t for t in traces if t.get("status") != "succeeded"]
    totals = [t.get("totalMs", 0) for t in ok]
    queue = [t.get("queueMs", 0) + t.get("psQueueMs", 0) + t.get("ssQueueMs", 0) + t.get("downloadQueueMs", 0) for t in ok]
    expand = [t.get("expandMs", 0) for t in ok]
    sse = [t.get("sseMs", 0) for t in ok]
    download = [t.get("downloadMs", 0) for t in ok]
    unaccounted = [max(0, totals[i] - queue[i] - expand[i] - sse[i] - download[i]) for i in range(len(ok))]
    return {
        "count": len(traces),
        "ok": len(ok),
        "failed": len(fail),
        "total_ms": {"p50": pct(totals, 0.5), "p90": pct(totals, 0.9), "max": max(totals) if totals else None},
        "queue_ms": {"p50": pct(queue, 0.5), "p90": pct(queue, 0.9), "sum_p50_share": None},
        "expand_ms": {"p50": pct(expand, 0.5), "p90": pct(expand, 0.9)},
        "sse_ms": {"p50": pct(sse, 0.5), "p90": pct(sse, 0.9)},
        "download_ms": {"p50": pct(download, 0.5), "p90": pct(download, 0.9)},
        "other_ms_p50": pct(unaccounted, 0.5),
        "phase_share_p50": {
            "queue": round(pct(queue, 0.5) / pct(totals, 0.5) * 100, 1) if pct(totals, 0.5) else 0,
            "expand": round(pct(expand, 0.5) / pct(totals, 0.5) * 100, 1) if pct(totals, 0.5) else 0,
            "sse": round(pct(sse, 0.5) / pct(totals, 0.5) * 100, 1) if pct(totals, 0.5) else 0,
            "download": round(pct(download, 0.5) / pct(totals, 0.5) * 100, 1) if pct(totals, 0.5) else 0,
        },
        "failures": [
            {
                "requestId": t.get("requestId"),
                "status": t.get("status"),
                "totalMs": t.get("totalMs"),
                "sseMs": t.get("sseMs"),
                "error": t.get("errorCode"),
            }
            for t in fail
        ],
    }


def main() -> None:
    login = api("POST", "/api/admin/v1/auth/login", body={"username": "admin", "password": load_password()})
    token = login["tokens"]["accessToken"]
    data = api("GET", f"/api/admin/v1/image-timeline?window={WINDOW}", token)
    traces = sorted(data.get("traces", []), key=lambda t: t.get("startedAt", ""))
    snap = data.get("snapshot", {})
    # 最近 10 / 20 条（对应两轮 canary 规模）
    last10 = summarize(traces[-10:])
    last20 = summarize(traces[-20:])
    last30 = summarize(traces[-30:])
    report = {
        "window": WINDOW,
        "snapshot": {
            "promptSlots": snap.get("promptSlots"),
            "sseSlots": snap.get("sseSlots"),
            "queueCapacity": snap.get("queueCapacity"),
            "successRate": snap.get("successRate"),
            "p90TotalMs": snap.get("p90TotalMs"),
        },
        "last_10_traces": last10,
        "last_20_traces": last20,
        "last_30_traces": last30,
        "note": "wall_seconds ≈ max(total_ms) under full slot parallelism; queue_*_ms is local FIFO wait",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
