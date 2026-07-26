#!/usr/bin/env python3
"""Audit Chrome ticket pool vs Imagine quota for image dispatch pool only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from chrome_ticket_experiment_lib import (  # noqa: E402
    image_dispatch_account_ids,
    resolve_mint_accounts,
    runtime_image_dispatch_account_ids,
    ticket_quota_audit,
    unified_pool_snapshot,
    web_lane_quota_summary,
    web_pools_snapshot,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Chrome ticket pool quota audit (image dispatch pool only)")
    parser.add_argument("--account-ids", default="dispatch", help="dispatch or comma-separated ids (must be in dispatch pool)")
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=None,
        help="concurrent quota fetch workers on Panda (default: CHROME_TICKET_QUOTA_WORKERS or 12)",
    )
    parser.add_argument("--sso-file", type=Path, default=TOOLS.parent / ".tmp" / "web-sso-canary-1467.json")
    args = parser.parse_args()

    pools = web_pools_snapshot()
    quota = web_lane_quota_summary()
    raw = (args.account_ids or "").strip().lower()
    if raw == "dispatch":
        ids = resolve_mint_accounts(None, sso_file=args.sso_file)
    else:
        explicit = [int(x) for x in args.account_ids.split(",") if x.strip()]
        ids = resolve_mint_accounts(explicit, sso_file=args.sso_file)

    snapshot = unified_pool_snapshot(workers=args.workers)
    pools = snapshot.get("web_pools") or pools
    quota = snapshot.get("lane_quota") or quota
    audit = ticket_quota_audit(
        ids,
        include_dispatch_without_tickets=True,
        workers=args.workers,
        snapshot=snapshot,
    )
    violations = [row for row in audit if not row["ok"]]
    report = {
        "mint_scope": "image_dispatch_pool_only",
        "image_dispatch_pool_ids": image_dispatch_account_ids(),
        "runtime_dispatch_ids_diagnostic": runtime_image_dispatch_account_ids(pools),
        "web_pools": {
            "fourPools": pools.get("fourPools"),
            "selectionDiagnostics": pools.get("selectionDiagnostics"),
            "pinNotInDispatch": pools.get("pinNotInDispatch"),
        },
        "web_lane_quota_diagnostic": quota,
        "mint_scope_with_sso": ids,
        "rows": audit,
        "violations": violations,
        "all_ok": len(violations) == 0,
        "note": "仅图轨 dispatch 调度池灌票/对账；normal/verification/delete 不会被 Selector 消费票",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
