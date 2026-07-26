#!/usr/bin/env python3
"""Test meta re-push after delays: immediate, 1m, 5m, 30m between first and second consume."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from chrome_ticket_experiment_lib import (  # noqa: E402
    DEFAULT_LOG,
    DEFAULT_SSO,
    append_experiment_log,
    ensure_pin_scripts,
    log_event,
    mint_to_pool,
    parse_duration,
    pin_imagine_accounts,
    push_ticket,
    sleep_countdown,
    verdict,
)
from chrome_ticket_survival_runner import detailed_probe  # noqa: E402


def run_delay_variant(delay: str, args: argparse.Namespace) -> int:
    batch = f"R-delay-{delay}"
    ensure_pin_scripts()
    pin_imagine_accounts([args.account_id])
    minted = mint_to_pool(args.account_id, Path(args.sso_file), timeout=args.mint_timeout)
    first = detailed_probe(log_since=300)
    if args.stop_on_fail and first.get("http") != 200:
        append_experiment_log(batch, log_path=Path(args.log), stage="first", probe=first, result="fail")
        return 1

    wait_s = parse_duration(delay)
    sleep_countdown(wait_s, label=f"reuse-gap-{delay}")

    repush = push_ticket(minted["row"], ttl_hours=args.ttl_hours)
    second = detailed_probe(log_since=max(wait_s + 60, 120))
    result = verdict(second.get("http", 0), second.get("pool_hit", False))
    row = append_experiment_log(
        batch,
        log_path=Path(args.log),
        account_id=args.account_id,
        gap_s=wait_s,
        first_probe=first,
        second_probe=second,
        repush_ticket_id=repush.get("id"),
        result=result,
        notes="pool record single-use; meta re-push after gap",
    )
    print(json.dumps(row, ensure_ascii=False))
    if args.stop_on_fail and result != "pass":
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", default="0", help="gap before re-push: 0, 1m, 5m, 30m")
    parser.add_argument("--all", action="store_true", help="run 0,1m,5m,30m sequentially")
    parser.add_argument("--account-id", type=int, default=1467)
    parser.add_argument("--sso-file", default=str(DEFAULT_SSO))
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--ttl-hours", type=float, default=24.0)
    parser.add_argument("--mint-timeout", type=int, default=120)
    parser.add_argument("--stop-on-fail", action="store_true", default=True)
    args = parser.parse_args()

    delays = ["0", "1m", "5m", "30m"] if args.all else [args.delay]
    for delay in delays:
        log_event("reuse_delay_start", delay=delay)
        rc = run_delay_variant(delay, args)
        if rc != 0:
            log_event("reuse_delay_stop", failed_at=delay)
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
