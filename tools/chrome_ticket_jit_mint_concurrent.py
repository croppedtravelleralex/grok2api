#!/usr/bin/env python3
"""Concurrent JIT mint: preflight gate → N workers → target ticket count."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


def _init_mint_worker(admin_tok: str) -> None:
    os.environ["PANDA_ADMIN_TOKEN"] = admin_tok
    os.environ.setdefault("GROK_PW_HEADLESS", "1")


def _jit_mint_worker(account_id: int, timeout: int) -> dict:
    from chrome_ticket_experiment_lib import jit_mint_one

    t0 = time.time()
    row = jit_mint_one(int(account_id), timeout=timeout)
    row["wall_s"] = round(time.time() - t0, 1)
    return row


def build_mint_plan(work: list[dict], total: int) -> list[int]:
    """Round-robin across worklist headroom until total tickets planned."""
    if total <= 0 or not work:
        return []
    slots: list[tuple[int, int]] = [(int(w["account_id"]), int(w.get("headroom") or 0)) for w in work]
    slots = [(aid, h) for aid, h in slots if h > 0]
    if not slots:
        return []
    plan: list[int] = []
    idx = 0
    remaining = {aid: h for aid, h in slots}
    order = [aid for aid, _ in slots]
    while len(plan) < total and any(remaining.get(aid, 0) > 0 for aid in order):
        aid = order[idx % len(order)]
        if remaining.get(aid, 0) > 0:
            plan.append(aid)
            remaining[aid] -= 1
        idx += 1
        if idx > total * len(order) * 2:
            break
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description="Concurrent JIT Chrome ticket mint")
    parser.add_argument("-n", "--count", type=int, default=30, help="target tickets to mint")
    parser.add_argument("-j", "--workers", type=int, default=6, help="parallel mint workers (max 6)")
    parser.add_argument("--mint-timeout", type=int, default=180)
    parser.add_argument("-w", "--quota-workers", type=int, default=16, help="unified snapshot workers")
    parser.add_argument("--no-sync-pins", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("GROK_PW_HEADLESS", "1")

    from chrome_ticket_experiment_lib import (  # noqa: E402
        admin_token,
        dispatch_mint_worklist,
        log_event,
        pool_stats,
        preflight_mint_gate,
        sync_image_dispatch_pins,
        unified_pool_snapshot,
    )

    workers = max(1, min(int(args.workers), 6))
    target = max(1, int(args.count))

    gate = preflight_mint_gate(workers=args.quota_workers, sync_pins=not args.no_sync_pins)
    report = gate["report"]
    snapshot = gate["snapshot"]
    audit = gate["audit"]
    if not report.get("ok"):
        log_event("concurrent_mint_gate_warn", **{k: report.get(k) for k in ("violations", "orphans", "pin_not_in_dispatch")})

    dispatch = report.get("mintable_dispatch") or []
    imagine_cache = {int(r["account_id"]): int(r["imagine_remaining"]) for r in audit}
    stats = snapshot.get("pool_stats") or {}
    before_available = int(
        (stats.get("ByStatus") or stats.get("byStatus") or {}).get("available") or 0
    )
    per_account = max(1, (target + max(len(dispatch), 1) - 1) // max(len(dispatch), 1))
    work = dispatch_mint_worklist(
        per_account,
        dispatch_ids=dispatch,
        imagine_cache=imagine_cache,
        pool_stats_snapshot=stats,
    )
    blocked = set(report.get("blocked_account_ids") or [])
    if blocked:
        work = [w for w in work if int(w["account_id"]) not in blocked]

    plan = build_mint_plan(work, target)
    if not plan:
        out = {
            "ok": False,
            "reason": "empty_mint_plan",
            "dispatch": len(dispatch),
            "work_count": len(work),
            "before_available": before_available,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    plan = plan[:target]
    summary = {
        "target": target,
        "workers": workers,
        "planned": len(plan),
        "plan_accounts": dict(Counter(plan)),
        "before_available": before_available,
        "gate_ok": report.get("ok"),
        "work_head": work[:10],
    }
    log_event("concurrent_mint_start", **{k: v for k, v in summary.items() if k != "work_head"})
    print(json.dumps({"phase": "plan", **summary}, ensure_ascii=False, indent=2))

    if args.dry_run:
        return 0

    if not args.no_sync_pins:
        sync_image_dispatch_pins()

    t0 = time.time()
    shared_token = admin_token()
    results: list[dict] = []
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_mint_worker,
        initargs=(shared_token,),
    ) as pool:
        futs = {
            pool.submit(_jit_mint_worker, aid, args.mint_timeout): (i, aid)
            for i, aid in enumerate(plan, 1)
        }
        for fut in as_completed(futs):
            seq, aid = futs[fut]
            try:
                row = fut.result()
            except Exception as exc:
                row = {"ok": False, "account_id": aid, "error": str(exc)[:300], "seq": seq}
            row["seq"] = seq
            results.append(row)
            log_event(
                "concurrent_mint_done",
                seq=seq,
                account_id=aid,
                ok=row.get("ok"),
                pool_delta=row.get("pool_delta"),
                wall_s=row.get("wall_s"),
            )
            print(
                f"[{len(results)}/{len(plan)}] aid={aid} ok={row.get('ok')} "
                f"delta={row.get('pool_delta')} wall={row.get('wall_s')}s"
            )

    results.sort(key=lambda r: int(r.get("seq") or 0))
    ok_count = sum(1 for r in results if r.get("ok"))
    try:
        after_stats = pool_stats()
        after_available = int(
            (after_stats.get("ByStatus") or after_stats.get("byStatus") or {}).get("available") or 0
        )
    except Exception as exc:
        after_available = None
        after_stats = {"error": str(exc)[:200]}

    final = {
        "ok": ok_count >= target,
        "mint_ok": ok_count,
        "mint_fail": len(results) - ok_count,
        "target": target,
        "workers": workers,
        "wall_total_s": round(time.time() - t0, 1),
        "before_available": before_available,
        "after_available": after_available,
        "pool_delta": (after_available - before_available) if after_available is not None else None,
        "results": results,
        "pool_stats_after": after_stats.get("ByStatus") or after_stats.get("byStatus"),
    }
    log_event("concurrent_mint_summary", mint_ok=ok_count, target=target, after_available=after_available)
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return 0 if ok_count >= target else 1


if __name__ == "__main__":
    raise SystemExit(main())
