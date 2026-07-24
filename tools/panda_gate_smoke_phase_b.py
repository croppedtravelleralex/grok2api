#!/usr/bin/env python3
"""Phase B gate smoke: pin -> reconcile -> N x single-concurrency image probe.

Run on Panda after deploying BE-019/018/021 build.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ADMIN_BASE = "http://127.0.0.1:18000/api/admin/v1"
GEN_BASE = "http://127.0.0.1:18000"
KEY_FILE = Path("/root/.secrets/grok2api-newapi-key")
PIN_SCRIPT = Path("/tmp/_panda_pin_imagine.py")
PREPARE_SCRIPT = Path("/tmp/_panda_prepare_imagine.py")
PROBE_SCRIPT = Path("/tmp/_panda_image_probe.py")


def admin_token() -> str:
    proc = subprocess.run(["python3", str(Path("/tmp/_panda_admin_token.py"))], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def admin_get(path: str, tok: str) -> dict:
    req = urllib.request.Request(f"{ADMIN_BASE}{path}", headers={"Authorization": f"Bearer {tok}"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode())


def admin_post(path: str, tok: str, body: dict | None = None) -> dict:
    data = b"{}" if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{ADMIN_BASE}{path}",
        data=data,
        method="POST",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=180).read().decode())


def run_probe() -> dict:
    proc = subprocess.run(["python3", str(PROBE_SCRIPT)], capture_output=True, text=True, check=True)
    return json.loads(proc.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase B gate smoke on Panda")
    parser.add_argument("account_ids", nargs="+", type=int, help="accounts to pin, e.g. 1574 1507 1467 92")
    parser.add_argument("--runs", type=int, default=10, help="single-concurrency image probes")
    parser.add_argument("--sleep", type=float, default=2.0, help="seconds between probes")
    args = parser.parse_args()

    report: dict = {"account_ids": args.account_ids, "checks": {}, "probes": []}

    # prepare already pins; skip duplicate pin call here
    prep = subprocess.run(
        ["python3", str(PREPARE_SCRIPT), *[str(x) for x in args.account_ids]],
        capture_output=True,
        text=True,
        check=True,
    )
    prep_lines = [line.strip() for line in prep.stdout.splitlines() if line.strip()]
    prep_data = json.loads(prep_lines[-1])
    report["prepare"] = prep_data

    tok = admin_token()
    pools = admin_get("/accounts/web-pools", tok).get("data", {})
    report["web_pools"] = {
        "imagePoolIds": pools.get("imagePoolIds"),
        "pinNotInDispatch": pools.get("pinNotInDispatch"),
        "selectionDiagnostics": pools.get("selectionDiagnostics"),
    }

    missing = [aid for aid in args.account_ids if aid not in set(pools.get("imagePoolIds") or [])]
    pin_not_in = pools.get("pinNotInDispatch") or []
    report["checks"]["pin_in_dispatch"] = len(missing) == 0 and len(pin_not_in) == 0

    # 2) N x single-concurrency probes
    ok = 0
    err503 = 0
    err429 = 0
    other = 0
    request_ids: list[str] = []
    for i in range(args.runs):
        row = run_probe()
        row["run"] = i + 1
        report["probes"].append(row)
        http = int(row.get("http") or 0)
        if http == 200:
            ok += 1
        elif http == 503:
            err503 += 1
        elif http == 429:
            err429 += 1
        else:
            other += 1
        if i + 1 < args.runs:
            time.sleep(args.sleep)

    report["summary"] = {
        "ok": ok,
        "total": args.runs,
        "http_503": err503,
        "http_429": err429,
        "other": other,
    }
    report["checks"]["no_503"] = err503 == 0
    report["checks"]["image_ok"] = ok >= 1

    # 3) egress traffic (first successful probe with request id in audit - best effort)
    try:
        audits = admin_get("/audits?limit=5&sortBy=createdAt&sortOrder=desc", tok).get("data", {})
        items = audits.get("items") or audits if isinstance(audits, list) else []
        for item in items[:5]:
            rid = item.get("requestId") or item.get("request_id")
            if not rid:
                continue
            try:
                hops = admin_get(f"/egress-traffic?requestId={rid}", tok).get("data", {})
                if hops.get("items"):
                    report["egress_traffic_sample"] = hops
                    report["checks"]["egress_hops_visible"] = len(hops.get("items") or []) > 0
                    break
            except urllib.error.HTTPError:
                continue
    except Exception as exc:
        report["egress_traffic_error"] = str(exc)[:200]

    passed = all(report["checks"].get(k) for k in ("pin_in_dispatch", "no_503", "image_ok"))
    report["gate_passed"] = passed
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
