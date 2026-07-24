#!/usr/bin/env python3
"""Faster ticket minting: reuse one browser context across accounts, optional parallel workers."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from chrome_ticket_experiment_lib import effective_mint_target, load_accounts, log_event, mint_headroom, push_ticket  # noqa: E402


def mint_one_process(account_id: int, sso_file: str, timeout: int) -> dict:
    if sys.platform == "win32" and "GROK_PW_HEADLESS" not in os.environ:
        os.environ["GROK_PW_HEADLESS"] = "1"
    from chrome_ticket_experiment_lib import mint_ticket_row

    t0 = time.perf_counter()
    row = mint_ticket_row(account_id, Path(sso_file), timeout=timeout)
    pushed = push_ticket(row)
    return {
        "account_id": account_id,
        "ticket_id": pushed.get("id"),
        "mint_s": round(time.perf_counter() - t0, 2),
        "meta_len": len(row.get("statsig_meta") or ""),
    }


def mint_sequential(account_ids: list[int], sso_file: Path, timeout: int) -> list[dict]:
    """Reuse browser: one playwright session, multiple pages (future). For now sequential with headless."""
    out = []
    for aid in account_ids:
        out.append(mint_one_process(aid, str(sso_file), timeout))
        log_event("fast_mint_ok", **out[-1])
    return out


def mint_parallel(account_ids: list[int], sso_file: Path, timeout: int, workers: int) -> list[dict]:
    out = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(mint_one_process, aid, str(sso_file), timeout): aid for aid in account_ids}
        for fut in as_completed(futs):
            row = fut.result()
            out.append(row)
            log_event("fast_mint_ok", **row)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Accelerated Chrome ticket minting")
    parser.add_argument("--account-ids", required=True, help="comma-separated")
    parser.add_argument("--sso-file", type=Path, default=ROOT / ".tmp" / "web-sso-canary-1467.json")
    parser.add_argument("--workers", type=int, default=1, help="parallel browser processes (up to 6)")
    parser.add_argument("--target-per-account", type=int, default=0, help="cap mint count per account by Imagine quota (0=unlimited batch)")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--headless", action="store_true", default=True)
    args = parser.parse_args()
    if args.headless:
        os.environ["GROK_PW_HEADLESS"] = "1"
    ids = [int(x.strip()) for x in args.account_ids.split(",") if x.strip()]
    accounts = load_accounts(args.sso_file)
    ids = [i for i in ids if i in accounts]
    if args.target_per_account > 0:
        capped: list[int] = []
        for aid in ids:
            headroom = mint_headroom(aid, args.target_per_account)
            if headroom <= 0:
                log_event("fast_mint_skip", account_id=aid, reason="quota_or_depth", target=args.target_per_account)
                continue
            capped.extend([aid] * headroom)
        ids = capped
    if not ids:
        print(json.dumps({"count": 0, "reason": "no_mint_headroom"}, ensure_ascii=False))
        return 0
    t0 = time.perf_counter()
    if args.workers <= 1:
        rows = mint_sequential(ids, args.sso_file, args.timeout)
    else:
        rows = mint_parallel(ids, args.sso_file, args.timeout, min(args.workers, 6))
    summary = {
        "count": len(rows),
        "total_s": round(time.perf_counter() - t0, 2),
        "avg_s": round((time.perf_counter() - t0) / max(len(rows), 1), 2),
        "rows": rows,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
