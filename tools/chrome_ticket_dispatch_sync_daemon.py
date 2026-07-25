#!/usr/bin/env python3
"""Keep image dispatch pin/runtime aligned with four-pool dispatch + Imagine quota.

有额度且 imageDispatchAdmissible → 自动上调度池（pin + runtime dispatchIndex）
额度耗尽或不再准入 → 自动下调度池
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
    image_dispatch_target_ids,
    log_event,
    runtime_image_dispatch_account_ids,
    sync_image_dispatch_pins,
    web_pools_snapshot,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto sync image dispatch pool pins with quota")
    parser.add_argument("--interval", type=int, default=300, help="seconds between sync ticks")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-api", action="store_true", help="force SSH pin fallback (pre-deploy)")
    args = parser.parse_args()

    while True:
        try:
            before = web_pools_snapshot()
            report = sync_image_dispatch_pins(dry_run=args.dry_run, use_api=not args.no_api)
            after = web_pools_snapshot() if not args.dry_run else before
            runtime_before = runtime_image_dispatch_account_ids(before)
            runtime_after = runtime_image_dispatch_account_ids(after)
            targets = image_dispatch_target_ids()
            tick = {
                "changed": report.get("changed"),
                "mode": report.get("mode"),
                "target_count": len(targets),
                "added": report.get("added") or [],
                "removed": report.get("removed") or [],
                "runtime_before": runtime_before,
                "runtime_after": runtime_after,
                "four_pools_image": (after.get("fourPools") or {}).get("image"),
            }
            log_event("dispatch_sync_tick", **tick)
            print(json.dumps({"report": report, "tick": tick}, ensure_ascii=False, indent=2))
        except Exception as exc:
            log_event("dispatch_sync_tick_error", error=str(exc)[:300])
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            if args.once:
                return 1
        if args.once:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
