#!/usr/bin/env python3
"""One-shot: reconcile dispatch pool → JIT mint → image generation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from chrome_ticket_experiment_lib import (  # noqa: E402
    PANDA_BASE,
    admin_token,
    dispatch_mint_worklist,
    image_dispatch_account_ids,
    imagine_quota_cap,
    imagine_remaining_by_account,
    jit_mint_one,
    log_event,
    panda_image_once,
    pin_imagine_accounts,
    runtime_image_dispatch_account_ids,
    ssh_run,
    web_pools_snapshot,
)


def ensure_runtime_dispatch_has_quota(work: list[dict], *, pin_size: int = 4) -> list[int]:
    """若 runtime pin 号无额度，改 pin 有额度的调度池账号（没额度不能接流量）。"""
    pools = web_pools_snapshot()
    runtime = runtime_image_dispatch_account_ids(pools)
    if not runtime:
        return runtime
    imagine = imagine_remaining_by_account(runtime)
    runtime_with_quota = [aid for aid in runtime if imagine.get(aid, 0) > 0]
    if runtime_with_quota:
        return runtime_with_quota
    candidates = [int(row["account_id"]) for row in work if int(row.get("imagine_cap") or 0) > 0]
    if not candidates:
        return runtime
    new_pin = candidates[:pin_size]
    log_event("runtime_pin_retarget", old=runtime, new=new_pin, reason="no_quota_on_runtime")
    pin_imagine_accounts(new_pin)
    return runtime_image_dispatch_account_ids(web_pools_snapshot())


def reconcile_web_pools() -> dict:
    token = admin_token()
    proc = ssh_run(
        f"curl -fsS -X POST {PANDA_BASE}/api/admin/v1/accounts/web-pools/reconcile "
        f"-H 'Authorization: Bearer {token}' -H 'Content-Type: application/json' -d '{{}}'",
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"reconcile failed: {(proc.stderr or proc.stdout)[:400]}")
    payload = json.loads(proc.stdout)
    return payload.get("data") or payload


def pick_mint_account(work: list[dict], runtime_ids: list[int]) -> int | None:
    runtime = set(runtime_ids)
    for item in work:
        aid = int(item["account_id"])
        if runtime and aid not in runtime:
            continue
        return aid
    return int(work[0]["account_id"]) if work else None


def main() -> int:
    parser = argparse.ArgumentParser(description="JIT mint + image e2e once")
    parser.add_argument("--target", type=int, default=1)
    parser.add_argument("--mint-timeout", type=int, default=120)
    parser.add_argument("--skip-reconcile", action="store_true")
    parser.add_argument("--skip-image", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("GROK_PW_HEADLESS", "1")

    report: dict = {}
    if not args.skip_reconcile:
        report["reconcile"] = reconcile_web_pools()
    pools = web_pools_snapshot()
    dispatch = image_dispatch_account_ids(force_refresh=True)
    work = dispatch_mint_worklist(args.target, dispatch_ids=dispatch)
    runtime = ensure_runtime_dispatch_has_quota(work)
    report["dispatch_with_quota"] = dispatch
    report["runtime_dispatch"] = runtime
    report["worklist"] = work
    if not work:
        print(json.dumps({"ok": False, "stage": "worklist_empty", **report}, ensure_ascii=False, indent=2))
        return 1
    aid = pick_mint_account(work, runtime)
    if aid is None:
        print(json.dumps({"ok": False, "stage": "no_account", **report}, ensure_ascii=False, indent=2))
        return 1
    report["mint_account_id"] = aid
    mint = jit_mint_one(aid, timeout=args.mint_timeout)
    report["mint"] = mint
    if not mint.get("ok"):
        print(json.dumps({"ok": False, **report}, ensure_ascii=False, indent=2))
        return 1
    if not args.skip_image:
        image = panda_image_once()
        report["image"] = image
        report["ok"] = image.get("http") == 200 and image.get("pool_hit")
    else:
        report["ok"] = True
    log_event("jit_e2e_once", ok=report.get("ok"), account_id=aid)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
