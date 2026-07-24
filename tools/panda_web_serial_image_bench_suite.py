#!/usr/bin/env python3
"""Multi-suite serial Web image benchmark: 3×10 rounds (or until tickets exhausted).

Records wall time, pipeline stages, egress hop bytes, ticket pool deltas.
Never aborts on failure within a suite.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = os.environ.get("GROK2API_BASE", "http://127.0.0.1:18000").rstrip("/")
OUT = Path(os.environ.get("GROK2API_OUT", "/opt/grok2api/data/image-bench"))
SUITES = int(os.environ.get("GROK2API_BENCH_SUITES", "3"))
ROUNDS_PER_SUITE = int(os.environ.get("GROK2API_BENCH_ROUNDS", "10"))
CONSUME_ALL = os.environ.get("GROK2API_BENCH_CONSUME_ALL", "1").strip().lower() not in {"0", "false", "no"}
ROUND_GAP_S = float(os.environ.get("GROK2API_BENCH_ROUND_GAP", "2"))
SUITE_GAP_S = float(os.environ.get("GROK2API_BENCH_SUITE_GAP", "5"))
PREFLIGHT_SYNC = os.environ.get("GROK2API_BENCH_PREFLIGHT_SYNC", "0").strip().lower() not in {"0", "false", "no"}

ROUNDS_SPEC = [
    ("grok-imagine-image", "zh_apple", "一只红苹果放在白色桌面上，产品摄影"),
    ("grok-imagine-image", "en_mug", "a red ceramic mug on white background, product photo"),
    ("grok-imagine-image", "zh_cat", "一只橘猫坐在窗台上，自然光"),
    ("grok-imagine-image", "en_cube", "blue cube on white surface, studio lighting"),
    ("grok-imagine-image", "zh_bike", "红色公路自行车，侧视图，干净背景"),
    ("grok-imagine-image", "zh_sunflower", "向日葵特写，浅景深，暖色调"),
    ("grok-imagine-image", "en_watch", "silver wristwatch on marble surface, macro product photo"),
    ("grok-imagine-image", "zh_mountain", "雪山日出，薄雾山谷，风光摄影"),
    ("grok-imagine-image", "en_penguin", "emperor penguin on ice, national geographic style"),
    ("grok-imagine-image", "zh_tea", "一杯热茶与书本，窗边自然光，静物"),
]


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * (p / 100.0)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return ordered[lo]
    weight = rank - lo
    return ordered[lo] * (1 - weight) + ordered[hi] * weight


def load_key() -> str:
    for path in (Path("/root/.secrets/grok2api-newapi-key"), Path("/opt/grok2api/staging/client-key")):
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    env = os.environ.get("GROK2API_KEY", "").strip()
    if env:
        return env
    raise SystemExit("client API key missing")


def load_admin_token() -> str:
    for path in (Path("/root/.secrets/grok2api-admin-password"),):
        if not path.exists():
            continue
        password = path.read_text(encoding="utf-8").strip()
        status, payload = admin_api("POST", "/api/admin/v1/auth/login", body={"username": "admin", "password": password})
        if status == 200:
            return payload["data"]["tokens"]["accessToken"]
    return ""


def admin_api(method: str, path: str, token: str = "", body: dict | None = None) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=180) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return resp.status, json.loads(raw) if raw else {}


def chrome_ticket_stats(token: str) -> dict:
    status, payload = admin_api("GET", "/api/admin/v1/chrome-tickets/stats", token=token)
    if status != 200:
        return {"error": status, "raw": payload}
    return payload.get("data") or payload


def egress_traffic(token: str, request_id: str) -> dict:
    if not token or not request_id:
        return {}
    status, payload = admin_api("GET", f"/api/admin/v1/egress-traffic?requestId={request_id}", token=token)
    if status != 200:
        return {"error": status, "raw": payload}
    return payload.get("data") or payload


def client_post(path: str, key: str, body: dict, timeout: float = 300) -> tuple[int, dict, float, int]:
    started = time.perf_counter()
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            elapsed = time.perf_counter() - started
            text = raw.decode("utf-8", errors="replace")
            parsed = json.loads(text) if text else {}
            return resp.status, parsed, elapsed, len(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        elapsed = time.perf_counter() - started
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text) if text else {"error": text}
        except json.JSONDecodeError:
            parsed = {"error": text}
        return exc.code, parsed, elapsed, len(raw)


def find_audit(token: str, request_id: str, model: str) -> dict | None:
    if not token:
        return None
    status, payload = admin_api("GET", "/api/admin/v1/request-audits?page=1&pageSize=30", token=token)
    if status != 200:
        return None
    items = payload.get("data", {}).get("items", [])
    for item in items:
        if request_id and item.get("requestId") == request_id:
            return item
    for item in items:
        if item.get("modelPublicId") == model:
            return item
    return items[0] if items else None


def find_trace(token: str, request_id: str) -> dict | None:
    if not token or not request_id:
        return None
    status, payload = admin_api("GET", "/api/admin/v1/image-timeline?window=1h", token=token)
    if status != 200:
        return None
    for trace in payload.get("data", {}).get("traces", []):
        if trace.get("requestId") == request_id:
            return trace
    return None


def run_round(suite_idx: int, round_idx: int, model: str, name: str, prompt: str, key: str, admin_token: str) -> dict:
    body = {"model": model, "prompt": prompt, "n": 1}
    status, resp, elapsed, resp_bytes = client_post("/v1/images/generations", key, body)
    item = (resp.get("data") or [{}])[0] if isinstance(resp.get("data"), list) else {}
    request_id = (
        resp.get("request_id")
        or resp.get("id")
        or (resp.get("error") or {}).get("request_id")
        or ""
    )
    audit = find_audit(admin_token, request_id, model)
    trace = find_trace(admin_token, request_id or (audit or {}).get("requestId", ""))
    traffic = egress_traffic(admin_token, request_id or (audit or {}).get("requestId", ""))
    totals = (traffic or {}).get("totals") or {}
    hops = (traffic or {}).get("items") or []
    row = {
        "suite": suite_idx,
        "round": round_idx,
        "name": name,
        "model": model,
        "prompt": prompt,
        "http_status": status,
        "ok": status == 200 and bool(item.get("url") or item.get("b64_json")),
        "wall_seconds": round(elapsed, 3),
        "response_bytes": resp_bytes,
        "revised_prompt": item.get("revised_prompt") or "",
        "expand_chars": len(item.get("revised_prompt") or ""),
        "image_url_prefix": (item.get("url") or "")[:160],
        "error": "" if status == 200 else json.dumps(resp, ensure_ascii=False)[:800],
        "request_id": request_id or (audit or {}).get("requestId"),
        "audit": audit,
        "pipeline_trace": trace,
        "egress_traffic": traffic,
        "egress_request_bytes": int(totals.get("requestBytes") or 0),
        "egress_response_bytes": int(totals.get("responseBytes") or 0),
        "egress_hop_count": len(hops),
        "ps_queue_ms": (trace or {}).get("psQueueMs"),
        "ss_queue_ms": (trace or {}).get("ssQueueMs"),
        "expand_ms": (trace or {}).get("expandMs"),
        "sse_ms": (trace or {}).get("sseMs"),
        "download_ms": (trace or {}).get("downloadMs"),
        "total_ms": (trace or {}).get("totalMs"),
        "account": (trace or {}).get("accountName") or (audit or {}).get("accountName"),
        "account_id": (trace or {}).get("accountId") or (audit or {}).get("accountId"),
        "soft_stop": (trace or {}).get("softStop"),
    }
    return row


def summarize_rows(rows: list[dict]) -> dict:
    walls = [float(r["wall_seconds"]) for r in rows]
    egress_up = [float(r.get("egress_request_bytes") or 0) for r in rows]
    egress_down = [float(r.get("egress_response_bytes") or 0) for r in rows]
    expand_ms = [float(r["expand_ms"]) for r in rows if r.get("expand_ms") is not None]
    sse_ms = [float(r["sse_ms"]) for r in rows if r.get("sse_ms") is not None]
    download_ms = [float(r["download_ms"]) for r in rows if r.get("download_ms") is not None]
    status_hist: dict[str, int] = {}
    for r in rows:
        key = str(r.get("http_status"))
        status_hist[key] = status_hist.get(key, 0) + 1
    return {
        "count": len(rows),
        "success": sum(1 for r in rows if r.get("ok")),
        "failed": sum(1 for r in rows if not r.get("ok")),
        "status_histogram": status_hist,
        "wall_seconds": {
            "min": min(walls) if walls else 0,
            "max": max(walls) if walls else 0,
            "mean": round(statistics.mean(walls), 3) if walls else 0,
            "p50": round(pct(walls, 50), 3),
            "p95": round(pct(walls, 95), 3),
            "p99": round(pct(walls, 99), 3),
        },
        "egress_bytes": {
            "request_total": int(sum(egress_up)),
            "response_total": int(sum(egress_down)),
            "request_p50": int(pct(egress_up, 50)),
            "request_p95": int(pct(egress_up, 95)),
            "response_p50": int(pct(egress_down, 50)),
            "response_p95": int(pct(egress_down, 95)),
        },
        "pipeline_ms": {
            "expand_p50": round(pct(expand_ms, 50), 1) if expand_ms else None,
            "expand_p95": round(pct(expand_ms, 95), 1) if expand_ms else None,
            "sse_p50": round(pct(sse_ms, 50), 1) if sse_ms else None,
            "sse_p95": round(pct(sse_ms, 95), 1) if sse_ms else None,
            "download_p50": round(pct(download_ms, 50), 1) if download_ms else None,
            "download_p95": round(pct(download_ms, 95), 1) if download_ms else None,
        },
    }


def available_total(stats: dict) -> int:
    by_status = stats.get("byStatus") or stats.get("ByStatus") or {}
    return int(by_status.get("available") or 0)


def run_suite(suite_idx: int, key: str, admin_token: str) -> tuple[list[dict], dict, dict]:
    tickets_before = chrome_ticket_stats(admin_token) if admin_token else {}
    rows: list[dict] = []
    print(f"[suite {suite_idx}] tickets_before={available_total(tickets_before)}", flush=True)
    for idx, (model, name, prompt) in enumerate(ROUNDS_SPEC[:ROUNDS_PER_SUITE], 1):
        print(f"[suite {suite_idx} round {idx}/{ROUNDS_PER_SUITE}] {name}", flush=True)
        row = run_round(suite_idx, idx, model, name, prompt, key, admin_token)
        rows.append(row)
        print(
            f"  ok={row['ok']} http={row['http_status']} wall={row['wall_seconds']}s "
            f"egress↓={row['egress_response_bytes']} account={row.get('account') or '-'}",
            flush=True,
        )
        if idx < ROUNDS_PER_SUITE:
            time.sleep(ROUND_GAP_S)
    tickets_after = chrome_ticket_stats(admin_token) if admin_token else {}
    suite_meta = {
        "suite": suite_idx,
        "tickets_before": tickets_before,
        "tickets_after": tickets_after,
        "tickets_consumed_estimate": max(0, available_total(tickets_before) - available_total(tickets_after)),
    }
    return rows, summarize_rows(rows), suite_meta


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    key = load_key()
    admin_token = load_admin_token()
    all_rows: list[dict] = []
    suite_summaries: list[dict] = []
    suite_meta: list[dict] = []

    planned_suites = SUITES
    suite_idx = 0
    while True:
        if suite_idx >= 20:
            print("[stop] safety cap 20 suites", flush=True)
            break
        avail_before = available_total(chrome_ticket_stats(admin_token)) if admin_token else 0
        if suite_idx >= planned_suites and (not CONSUME_ALL or avail_before <= 0):
            break
        suite_idx += 1
        rows, summary, meta = run_suite(suite_idx, key, admin_token)
        all_rows.extend(rows)
        suite_summaries.append(summary)
        suite_meta.append(meta)
        avail_after = available_total(chrome_ticket_stats(admin_token)) if admin_token else 0
        if suite_idx < planned_suites or (CONSUME_ALL and avail_after > 0):
            time.sleep(SUITE_GAP_S)

    tickets_final = chrome_ticket_stats(admin_token) if admin_token else {}
    report = {
        "stamp": stamp,
        "base": BASE,
        "suites_planned": planned_suites,
        "suites_run": len(suite_summaries),
        "rounds_per_suite": ROUNDS_PER_SUITE,
        "consume_all": CONSUME_ALL,
        "tickets_final": tickets_final,
        "overall": summarize_rows(all_rows),
        "suite_summaries": suite_summaries,
        "suite_meta": suite_meta,
        "results": all_rows,
    }
    out = OUT / f"serial-suite-{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    brief = {k: v for k, v in report.items() if k != "results"}
    print(json.dumps(brief, ensure_ascii=False, indent=2), flush=True)
    print(f"detail={out}", flush=True)


if __name__ == "__main__":
    main()
