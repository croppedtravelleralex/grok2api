#!/usr/bin/env python3
"""JIT Chrome ticket minter: live dispatch pool → ephemeral SSO → mint → push → discard.

No local SSO batch file. Each ticket is a self-contained receipt (account_id, ticket_id, pool_delta).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from chrome_ticket_experiment_lib import (  # noqa: E402
    dispatch_mint_worklist,
    image_dispatch_account_ids,
    jit_mint_one,
    log_event,
    pool_stats,
    preflight_mint_gate,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="JIT Chrome ticket minter (no local SSO file)")
    parser.add_argument("--target", type=int, default=0, help="min tickets per dispatch account; 0=auto")
    parser.add_argument("--sse-slots", type=int, default=10, help="auto target: ceil(sse_slots / dispatch_count)")
    parser.add_argument("--interval", type=int, default=300, help="seconds between pool scans")
    parser.add_argument("--mint-timeout", type=int, default=120)
    parser.add_argument("--max-per-tick", type=int, default=3, help="max tickets minted per daemon tick")
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=None,
        help="unified snapshot quota workers (preflight gate)",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="list worklist only, no SSO/mint")
    parser.add_argument("--no-sync-pins", action="store_true", help="skip dispatch pin sync before each tick")
    args = parser.parse_args()

    dispatch = image_dispatch_account_ids()
    base_target = args.target if args.target > 0 else max(1, (args.sse_slots + max(len(dispatch), 1) - 1) // max(len(dispatch), 1))

    if args.audit_only:
        gate = preflight_mint_gate(workers=args.workers, sync_pins=not args.no_sync_pins)
        print(
            json.dumps(
                {
                    "gate": gate["report"],
                    "audit": gate["audit"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if gate["report"]["ok"] else 1

    while True:
        try:
            gate = preflight_mint_gate(
                workers=args.workers,
                sync_pins=not args.no_sync_pins,
            )
            report = gate["report"]
            snapshot = gate["snapshot"]
            audit = gate["audit"]
            if not report["ok"]:
                log_event(
                    "jit_gate_blocked",
                    violations=len(report.get("violations") or []),
                    orphans=report.get("orphans"),
                    pin_not_in_dispatch=report.get("pin_not_in_dispatch"),
                )
            dispatch = report.get("mintable_dispatch") or []
            imagine_cache = {int(r["account_id"]): int(r["imagine_remaining"]) for r in audit}
            work = dispatch_mint_worklist(
                base_target,
                dispatch_ids=dispatch,
                imagine_cache=imagine_cache,
                pool_stats_snapshot=stats,
            )
            blocked = set(report.get("blocked_account_ids") or [])
            if blocked:
                work = [w for w in work if int(w["account_id"]) not in blocked]
            stats = snapshot.get("pool_stats") or pool_stats()
            log_event(
                "jit_tick",
                dispatch_count=len(dispatch),
                work_count=len(work),
                pool_stats=stats.get("ByStatus") or stats.get("byStatus"),
                target=base_target,
            )
            if args.dry_run:
                print(json.dumps({"work": work, "dispatch": dispatch}, ensure_ascii=False, indent=2))
                return 0
            tick_minted = 0
            receipts: list[dict] = []
            for item in work:
                if tick_minted >= args.max_per_tick:
                    break
                aid = int(item["account_id"])
                for _ in range(int(item["headroom"])):
                    if tick_minted >= args.max_per_tick:
                        break
                    receipt = jit_mint_one(aid, timeout=args.mint_timeout)
                    receipts.append(receipt)
                    tick_minted += 1
                    if not receipt.get("ok"):
                        break
            if receipts:
                log_event("jit_tick_done", minted=tick_minted, receipts=receipts)
        except Exception as exc:
            log_event("jit_tick_error", error=str(exc)[:300])
        if args.once:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
