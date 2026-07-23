#!/usr/bin/env python3
"""Serial Web image generation benchmark; stops on first failure and records diagnostics."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = os.environ.get("GROK2API_BASE", "http://127.0.0.1:18000").rstrip("/")
OUT = Path(os.environ.get("GROK2API_OUT", "/opt/grok2api/data/image-bench"))
ROUNDS = int(os.environ.get("GROK2API_BENCH_ROUNDS", "5"))
FAIL_FAST = os.environ.get("GROK2API_BENCH_FAIL_FAST", "1").strip().lower() not in {"0", "false", "no"}

ROUNDS_SPEC = [
    ("grok-imagine-image", "zh_apple", "一只红苹果放在白色桌面上，产品摄影"),
    ("grok-imagine-image", "en_mug", "a red ceramic mug on white background, product photo"),
    ("grok-imagine-image", "zh_cat", "一只橘猫坐在窗台上，自然光"),
    ("grok-imagine-image", "en_cube", "blue cube on white surface, studio lighting"),
    ("grok-imagine-image", "zh_bike", "红色公路自行车，侧视图，干净背景"),
]


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
    status, payload = admin_api("GET", "/api/admin/v1/request-audits?page=1&pageSize=20", token=token)
    if status != 200:
        return None
    items = payload.get("data", {}).get("items", [])
    for item in items:
        if request_id and item.get("requestId") == request_id:
            return item
    for item in items:
        if item.get("modelPublicId") == model or model in str(item.get("modelUpstreamModel", "")):
            return item
    return items[0] if items else None


def find_trace(token: str, request_id: str) -> dict | None:
    if not token or not request_id:
        return None
    status, payload = admin_api("GET", "/api/admin/v1/image-timeline?window=30m", token=token)
    if status != 200:
        return None
    for trace in payload.get("data", {}).get("traces", []):
        if trace.get("requestId") == request_id:
            return trace
    return None


def diagnose_failure(row: dict) -> dict:
    trace = row.get("pipeline_trace") or {}
    audit = row.get("audit") or {}
    return {
        "http_status": row.get("http_status"),
        "error": row.get("error"),
        "request_id": trace.get("requestId") or audit.get("requestId"),
        "trace_status": trace.get("status"),
        "trace_error": trace.get("errorCode"),
        "ps_queue_ms": trace.get("psQueueMs"),
        "ss_queue_ms": trace.get("ssQueueMs"),
        "expand_ms": trace.get("expandMs"),
        "sse_ms": trace.get("sseMs"),
        "soft_stop": trace.get("softStop"),
        "account": trace.get("accountName") or audit.get("accountName"),
        "audit_error": audit.get("errorCode"),
        "audit_status": audit.get("statusCode"),
    }


def run_round(idx: int, model: str, name: str, prompt: str, key: str, admin_token: str) -> dict:
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
    row = {
        "round": idx,
        "name": name,
        "model": model,
        "prompt": prompt,
        "http_status": status,
        "ok": status == 200 and bool(item.get("url") or item.get("b64_json")),
        "wall_seconds": round(elapsed, 3),
        "response_bytes": resp_bytes,
        "revised_prompt": item.get("revised_prompt") or "",
        "image_url_prefix": (item.get("url") or "")[:120],
        "error": "" if status == 200 else json.dumps(resp, ensure_ascii=False)[:800],
        "audit": audit,
        "pipeline_trace": trace,
        "raw_keys": list(resp.keys()),
    }
    if not row["ok"]:
        row["diagnosis"] = diagnose_failure(row)
    return row


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    key = load_key()
    admin_token = load_admin_token()
    results: list[dict] = []
    aborted = False
    print(f"starting {ROUNDS}-round serial bench base={BASE} fail_fast={FAIL_FAST}", flush=True)
    for idx, (model, name, prompt) in enumerate(ROUNDS_SPEC[:ROUNDS], 1):
        print(f"[round {idx}/{ROUNDS}] model={model} prompt={prompt[:40]}...", flush=True)
        row = run_round(idx, model, name, prompt, key, admin_token)
        results.append(row)
        account = ((row.get("pipeline_trace") or {}).get("accountName") or (row.get("audit") or {}).get("accountName") or "-")
        print(
            f"  ok={row['ok']} status={row['http_status']} wall={row['wall_seconds']}s "
            f"expand_len={len(row['revised_prompt'])} account={account}",
            flush=True,
        )
        if not row["ok"]:
            print("  diagnosis:", json.dumps(row.get("diagnosis", {}), ensure_ascii=False), flush=True)
            if FAIL_FAST:
                print(f"  aborting after round {idx} failure", flush=True)
                aborted = True
                break
        if idx < ROUNDS:
            time.sleep(2)

    walls = [r["wall_seconds"] for r in results]
    summary = {
        "stamp": stamp,
        "rounds_planned": ROUNDS,
        "rounds_run": len(results),
        "success": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
        "aborted": aborted,
        "p50_wall": sorted(walls)[len(walls) // 2] if walls else 0,
        "results": results,
    }
    out = OUT / f"serial-bench-{stamp}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, ensure_ascii=False, indent=2))
    print(f"detail={out}")
    if summary["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
