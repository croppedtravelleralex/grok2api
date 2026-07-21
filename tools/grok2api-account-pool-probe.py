#!/usr/bin/env python3
"""Cyclic account pool health probe for grok2api (Web/Console only).

Build 四池由进程内双探针维护；外挂 timer 不得对 Build 做 refresh-token /
refresh-billing，避免把删除池、验证池状态改写回 active。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("GROK2API_ADMIN_BASE", "http://127.0.0.1:18000/api/admin/v1")
PASSWORD_CANDIDATES = [
    Path(os.environ.get("GROK2API_ADMIN_PASSWORD_FILE", "")),
    Path("/root/.secrets/grok2api-admin-password"),
    Path("/opt/grok2api/staging/admin-password"),
]
# Build 由进程内 DispatchProbe + MaintenanceProbe 接管。
SKIP_PROVIDERS = {"grok_build", "build"}


def read_password() -> str:
    for path in PASSWORD_CANDIDATES:
        if str(path) and path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    raise SystemExit("admin password file missing; set GROK2API_ADMIN_PASSWORD_FILE")


def call(method: str, path: str, token: str = "", payload: dict | None = None, timeout: int = 120):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            data = json.loads(raw)
        except Exception:
            data = {"error": {"code": "non_json", "message": raw[:200].decode("utf-8", "replace")}}
        return exc.code, data
    return 200, json.loads(raw)


def unwrap(value):
    return value.get("data", value) if isinstance(value, dict) else value


def login() -> str:
    password = read_password()
    status, payload = call("POST", "/auth/login", payload={"username": "admin", "password": password})
    if status != 200:
        raise SystemExit(f"admin login failed: HTTP {status} {payload}")
    return unwrap(payload)["tokens"]["accessToken"]


def fetch_accounts(token: str) -> list[dict]:
    rows: list[dict] = []
    page = 1
    while True:
        status, payload = call("GET", f"/accounts?page={page}&pageSize=100", token=token)
        if status != 200:
            raise SystemExit(f"list accounts failed: HTTP {status}")
        data = unwrap(payload)
        rows.extend(data.get("items", []))
        if page * data.get("pageSize", 100) >= data.get("total", 0):
            break
        page += 1
    return rows


def provider_of(row: dict) -> str:
    return str(row.get("provider") or "").strip().lower()


def needs_probe(row: dict) -> bool:
    if provider_of(row) in SKIP_PROVIDERS:
        return False
    auth = row.get("authStatus")
    quota = row.get("quota") or {}
    quota_status = str(quota.get("status") or "")
    refreshable = bool(row.get("refreshable"))
    pool = str(row.get("pool") or "")
    if pool in {"delete", "verification", "dispatch"}:
        # 防御：即使 provider 字段异常，也不碰 Build 四池语义字段。
        if provider_of(row) in SKIP_PROVIDERS:
            return False
    if auth == "reauthRequired" and refreshable:
        return True
    if quota_status in {"pending", "isolated", "probing", "stale", "unknown", "waitingReset", "exhausted"}:
        return True
    if row.get("lastRefreshErrorCode") or row.get("lastError"):
        if auth != "active":
            return True
    return False


def probe_account(token: str, account_id: int, provider: str) -> dict:
    out = {"account_id": account_id, "provider": provider, "steps": []}
    steps = [("refresh_token", f"/accounts/{account_id}/refresh-token")]
    if provider in {"grok_web", "web"}:
        steps.append(("refresh_web_quota", f"/accounts/{account_id}/web/refresh-quota"))
    else:
        steps.append(("refresh_billing", f"/accounts/{account_id}/refresh-billing"))
    for step, path in steps:
        status, payload = call("POST", path, token=token, timeout=180)
        error = None
        if status != 200:
            body = unwrap(payload)
            error = body.get("error", body) if isinstance(body, dict) else body
        out["steps"].append({"step": step, "http": status, "error": error})
        if status == 200 and step == "refresh_token":
            continue
        if status != 200 and step == "refresh_token":
            break
    return out


def run_cycle(token: str, concurrency: int, limit: int = 0) -> dict:
    rows = fetch_accounts(token)
    skipped_build = sum(1 for row in rows if provider_of(row) in SKIP_PROVIDERS)
    targets = [row for row in rows if needs_probe(row)]
    if limit and limit > 0:
        targets = targets[:limit]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(5, concurrency))) as pool:
        futures = {
            pool.submit(probe_account, token, row["id"], provider_of(row)): row
            for row in targets
        }
        for fut in concurrent.futures.as_completed(futures):
            row = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001
                result = {"account_id": row["id"], "error": str(exc)}
            result["email"] = row.get("email") or row.get("name")
            result["auth_status"] = row.get("authStatus")
            results.append(result)
    return {
        "total_accounts": len(rows),
        "skipped_build": skipped_build,
        "probed": len(targets),
        "concurrency": concurrency,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="grok2api Web/Console pool cyclic probe (skips Build)")
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("GROK2API_PROBE_CONCURRENCY", "3")))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("GROK2API_PROBE_LIMIT", "0")), help="max accounts per cycle (0=all)")
    parser.add_argument("--loop", action="store_true", help="run forever")
    parser.add_argument("--interval", type=int, default=int(os.environ.get("GROK2API_PROBE_INTERVAL_SEC", "300")))
    args = parser.parse_args()
    concurrency = max(1, min(5, args.concurrency))

    while True:
        started = time.time()
        token = login()
        summary = run_cycle(token, concurrency, limit=args.limit)
        summary["elapsed_sec"] = round(time.time() - started, 2)
        summary["limit"] = args.limit
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        if not args.loop:
            break
        time.sleep(max(30, args.interval))
    return 0


if __name__ == "__main__":
    sys.exit(main())
