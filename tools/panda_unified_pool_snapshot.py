#!/usr/bin/env python3
"""Single-pass Panda snapshot: web-pools + ticket stats + lane-quota + quota batch.

Runs on Panda (127.0.0.1 admin API). One process, parallel HTTP inside the host.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

BASE = os.environ.get("PANDA_GROK2API_BASE", "http://127.0.0.1:18000").rstrip("/")


def admin_token() -> str:
    return subprocess.check_output(
        ["python3", "/tmp/_panda_admin_token.py"],
        text=True,
        timeout=90,
    ).strip()


def get_json(path: str, token: str) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else (payload if isinstance(payload, dict) else {})


def imagine_generations(remaining: int, total: int) -> int:
    if total <= 0 or remaining < 0:
        return 0
    if total <= 1000:
        return remaining
    unit = max(1, total // 10)
    return (remaining + unit - 1) // unit


def account_imagine_gens(acc: dict[str, Any]) -> int:
    for window in acc.get("quotaWindows") or []:
        if window.get("mode") != "imagine":
            continue
        total = int(window.get("total") or 0)
        remaining = int(window.get("remaining") or 0)
        return max(0, imagine_generations(remaining, total))
    return 0


def fetch_account_gens(aid: int, token: str) -> tuple[int, int]:
    try:
        acc = get_json(f"/api/admin/v1/accounts/{aid}", token)
        return aid, account_imagine_gens(acc)
    except Exception:
        return aid, 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified pool snapshot on Panda")
    parser.add_argument("--workers", type=int, default=12, help="concurrent account quota fetches")
    args = parser.parse_args()
    workers = max(1, min(int(args.workers), 32))
    token = admin_token()

    with ThreadPoolExecutor(max_workers=4) as pool:
        fut_pools = pool.submit(get_json, "/api/admin/v1/accounts/web-pools", token)
        fut_stats = pool.submit(get_json, "/api/admin/v1/chrome-tickets/stats", token)
        fut_lane = pool.submit(get_json, "/api/admin/v1/accounts/web-lane-quota", token)
        web_pools = fut_pools.result()
        pool_stats = fut_stats.result()
        lane_quota = fut_lane.result()

    dispatch_ids = sorted(
        {int(x) for x in (web_pools.get("imageDispatchPoolIds") or []) if int(x) > 0}
    )
    holder_ids: list[int] = []
    for item in pool_stats.get("AvailableByAccount") or pool_stats.get("availableByAccount") or []:
        aid = int(item.get("AccountID") or item.get("accountId") or item.get("account_id") or 0)
        if aid > 0:
            holder_ids.append(aid)

    schedulable_ids = sorted(
        {int(x) for x in (web_pools.get("imageSchedulableIds") or []) if int(x) > 0}
    )
    quota_ids = sorted(set(dispatch_ids) | set(holder_ids))
    fetch = partial(fetch_account_gens, token=token)
    imagine_by_account: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for aid, gens in pool.map(fetch, quota_ids):
            imagine_by_account[str(aid)] = int(gens)

    schedulable_not_dispatch = sorted(set(schedulable_ids) - set(dispatch_ids))
    dispatch_not_schedulable = sorted(set(dispatch_ids) - set(schedulable_ids))

    out = {
        "web_pools": web_pools,
        "pool_stats": pool_stats,
        "lane_quota": lane_quota,
        "imagine_by_account": imagine_by_account,
        "pool_diff": {
            "schedulable_not_dispatch": schedulable_not_dispatch,
            "dispatch_not_schedulable": dispatch_not_schedulable,
            "schedulable_count": len(schedulable_ids),
            "dispatch_count": len(dispatch_ids),
        },
        "meta": {
            "quota_account_count": len(quota_ids),
            "holder_count": len(holder_ids),
            "workers": workers,
        },
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
