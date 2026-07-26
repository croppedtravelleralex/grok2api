#!/usr/bin/env python3
"""Keep Chrome ticket pool depth above target (local minter + Panda stats).

Default: mint only for image dispatch pool accounts, capped by Imagine quota.
"""

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
    imagine_quota_cap,
    log_event,
    mint_headroom,
    parse_account_ids_from_arg,
    pool_available,
    pool_stats,
    resolve_mint_accounts,
    run_minter,
    ticket_quota_audit,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Continuous Chrome ticket minter daemon")
    parser.add_argument(
        "--account-ids",
        default="dispatch",
        help="comma-separated ids, or 'dispatch' for image dispatch pool only",
    )
    parser.add_argument("--sso-file", type=Path, default=DEFAULT_SSO)
    parser.add_argument("--target", type=int, default=0, help="min available tickets per account; 0=auto from dispatch/SSESlots")
    parser.add_argument("--sse-slots", type=int, default=10, help="used with --target 0: ceil(sse_slots / dispatch_count)")
    parser.add_argument("--interval", type=int, default=300, help="poll interval seconds")
    parser.add_argument("--mint-timeout", type=int, default=120)
    parser.add_argument("--once", action="store_true", help="top up once and exit")
    parser.add_argument("--audit-only", action="store_true", help="print quota audit and exit")
    args = parser.parse_args()
    if not args.sso_file.exists():
        log_event("daemon_abort", reason="sso_file_missing", path=str(args.sso_file))
        return 1

    ids = resolve_mint_accounts(parse_account_ids_from_arg(args.account_ids), sso_file=args.sso_file)
    if not ids:
        log_event("daemon_abort", reason="no_dispatch_accounts_with_sso")
        return 1

    if args.audit_only:
        audit = ticket_quota_audit(ids)
        print(__import__("json").dumps({"mint_scope": ids, "audit": audit}, ensure_ascii=False, indent=2))
        return 1 if any(not row["ok"] for row in audit) else 0

    def resolve_target() -> int:
        if args.target > 0:
            return args.target
        return max(1, (args.sse_slots + len(ids) - 1) // len(ids))

    while True:
        try:
            target = resolve_target()
            stats = pool_stats()
            violations = [r for r in ticket_quota_audit(ids) if not r["ok"]]
            if violations:
                log_event("daemon_quota_violation", violations=violations[:10])
            log_event("daemon_tick", stats=stats.get("ByStatus") or stats.get("byStatus"), target=target, accounts=ids)
            for aid in ids:
                cap = imagine_quota_cap(aid)
                headroom = mint_headroom(aid, target)
                avail = pool_available(aid)
                eff_target = effective_mint_target(aid, target)
                if headroom <= 0:
                    log_event(
                        "daemon_ok" if avail <= cap else "daemon_over_cap",
                        account_id=aid,
                        available=avail,
                        imagine_cap=cap,
                        target=eff_target,
                        reason="quota_or_depth",
                    )
                    continue
                log_event(
                    "daemon_mint",
                    account_id=aid,
                    available=avail,
                    imagine_cap=cap,
                    target=eff_target,
                    headroom=headroom,
                )
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
