#!/usr/bin/env python3
"""Keep Chrome ticket pool depth above target (local minter + Panda stats)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from chrome_ticket_experiment_lib import (
    DEFAULT_SSO,
    effective_mint_target,
    log_event,
    mint_headroom,
    pool_available,
    pool_stats,
    run_minter,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Continuous Chrome ticket minter daemon")
    parser.add_argument("--account-ids", default="1467", help="comma-separated account ids")
    parser.add_argument("--sso-file", type=Path, default=DEFAULT_SSO)
    parser.add_argument("--target", type=int, default=0, help="min available tickets per account; 0=auto from dispatch/SSESlots")
    parser.add_argument("--sse-slots", type=int, default=10, help="used with --target 0: ceil(sse_slots / dispatch_count)")
    parser.add_argument("--interval", type=int, default=300, help="poll interval seconds")
    parser.add_argument("--mint-timeout", type=int, default=120)
    parser.add_argument("--once", action="store_true", help="top up once and exit")
    args = parser.parse_args()
    ids = [int(x.strip()) for x in args.account_ids.split(",") if x.strip()]
    if not args.sso_file.exists():
        log_event("daemon_abort", reason="sso_file_missing", path=str(args.sso_file))
        return 1

    def resolve_target() -> int:
        if args.target > 0:
            return args.target
        if not ids:
            return 1
        return max(1, (args.sse_slots + len(ids) - 1) // len(ids))

    while True:
        try:
            target = resolve_target()
            stats = pool_stats()
            log_event("daemon_tick", stats=stats.get("ByStatus") or stats.get("byStatus"), target=target)
            for aid in ids:
                headroom = mint_headroom(aid, target)
                avail = pool_available(aid)
                cap = effective_mint_target(aid, target)
                if headroom <= 0:
                    log_event(
                        "daemon_ok" if avail >= cap else "daemon_skip",
                        account_id=aid,
                        available=avail,
                        target=cap,
                        reason="quota_or_depth",
                    )
                    continue
                log_event("daemon_mint", account_id=aid, available=avail, target=cap, headroom=headroom)
                ok = run_minter([aid], args.sso_file, timeout=args.mint_timeout)
                if not ok:
                    log_event("daemon_mint_failed", account_id=aid)
        except Exception as exc:
            log_event("daemon_error", error=str(exc)[:300])
        if args.once:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
