#!/usr/bin/env python3
"""Serial mint N tickets then serial image N — observe dispatch/scheduling."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from chrome_ticket_experiment_lib import (  # noqa: E402
    image_dispatch_account_ids,
    jit_mint_one,
    log_event,
    panda_image_once,
    pool_stats,
    run_remote_python,
    sync_image_dispatch_pins,
    web_pools_snapshot,
)


def last_image_account_id() -> int | None:
    script = r"""
import json, re, subprocess
logs=subprocess.run(["docker","logs","grok2api","--since","120s"], capture_output=True, text=True)
text=logs.stdout or ""
aid=None
for line in reversed(text.splitlines()):
    if "chrome_ticket_pool_hit" in line and "account_id" in line:
        m=re.search(r'"account_id"\s*:\s*(\d+)', line)
        if m: aid=int(m.group(1)); break
    if aid is None and "image_upstream" in line and "account_id" in line:
        m=re.search(r'"account_id"\s*:\s*(\d+)', line)
        if m: aid=int(m.group(1)); break
print(json.dumps({"account_id": aid}))
"""
    out = run_remote_python(script, remote_name="_serial_last_account.py", timeout=30)
    payload = json.loads(out.splitlines()[-1])
    aid = payload.get("account_id")
    return int(aid) if aid else None


def pick_mint_accounts(n: int) -> list[int]:
    dispatch = image_dispatch_account_ids(force_refresh=True)
    if not dispatch:
        return []
    return [dispatch[i % len(dispatch)] for i in range(n)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Serial mint + image benchmark")
    parser.add_argument("-n", "--count", type=int, default=20)
    parser.add_argument("--mint-timeout", type=int, default=180)
    parser.add_argument("--skip-sync", action="store_true")
    parser.add_argument("--mint-only", action="store_true")
    parser.add_argument("--image-only", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("GROK_PW_HEADLESS", "1")

    report: dict = {"count": args.count}
    if not args.skip_sync and not args.image_only:
        report["pin_sync"] = sync_image_dispatch_pins()
    report["pools_before"] = {
        "dispatch": len(image_dispatch_account_ids(force_refresh=True)),
        "snapshot": {
            "dispatch": len((web_pools_snapshot().get("imageDispatchPoolIds") or [])),
            "runtime": len((web_pools_snapshot().get("imagePoolIds") or [])),
            "pin": len((web_pools_snapshot().get("imagePinIds") or [])),
        },
        "pool_stats": pool_stats().get("ByStatus") or pool_stats().get("byStatus"),
    }

    mint_rows: list[dict] = []
    if not args.image_only:
        accounts = pick_mint_accounts(args.count)
        report["mint_plan"] = accounts
        for i, aid in enumerate(accounts, 1):
            t0 = time.time()
            row = jit_mint_one(aid, timeout=args.mint_timeout)
            row["seq"] = i
            row["wall_s"] = round(time.time() - t0, 1)
            mint_rows.append(row)
            log_event("serial_mint", seq=i, account_id=aid, ok=row.get("ok"), pool_delta=row.get("pool_delta"), ticket_id=(row.get("ticket_id") or "")[:16])
            print(f"[mint {i}/{args.count}] aid={aid} ok={row.get('ok')} delta={row.get('pool_delta')} ticket={row.get('ticket_id','')[:12]}")
            # 单张失败不中断，继续后续账号
        report["mint"] = mint_rows
        report["mint_ok"] = sum(1 for r in mint_rows if r.get("ok"))
        report["mint_accounts"] = dict(Counter(int(r["account_id"]) for r in mint_rows if r.get("ok")))

    image_rows: list[dict] = []
    if not args.mint_only:
        for i in range(1, args.count + 1):
            t0 = time.time()
            img = panda_image_once(log_since_s=30)
            img["seq"] = i
            img["wall_s"] = round(time.time() - t0, 1)
            img["account_id"] = last_image_account_id()
            image_rows.append(img)
            log_event(
                "serial_image",
                seq=i,
                http=img.get("http"),
                pool_hit=img.get("pool_hit"),
                account_id=img.get("account_id"),
                wall_s=img.get("wall_s"),
            )
            print(
                f"[image {i}/{args.count}] http={img.get('http')} pool_hit={img.get('pool_hit')} "
                f"aid={img.get('account_id')} wall={img.get('wall_s')}s"
            )
            if img.get("http") not in (200,):
                pass  # continue to observe scheduling under failures
        report["image"] = image_rows
        report["image_ok"] = sum(1 for r in image_rows if r.get("http") == 200)
        report["image_pool_hit"] = sum(1 for r in image_rows if r.get("pool_hit"))
        report["image_accounts"] = dict(Counter(int(r["account_id"]) for r in image_rows if r.get("account_id")))

    try:
        report["pools_after"] = {
            "pool_stats": pool_stats().get("ByStatus") or pool_stats().get("byStatus"),
            "snapshot": {
                "dispatch": len((web_pools_snapshot().get("imageDispatchPoolIds") or [])),
                "runtime": len((web_pools_snapshot().get("imagePoolIds") or [])),
            },
        }
    except Exception as exc:
        report["pools_after"] = {"error": str(exc)[:200]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    mint_ok = report.get("mint_ok", args.count if args.image_only else 0)
    image_ok = report.get("image_ok", args.count if args.mint_only else 0)
    target = 0 if args.mint_only else (0 if args.image_only else args.count)
    if args.mint_only:
        return 0 if mint_ok >= args.count else 1
    if args.image_only:
        return 0 if image_ok >= args.count else 1
    return 0 if mint_ok >= args.count and image_ok >= args.count else 1


if __name__ == "__main__":
    raise SystemExit(main())
