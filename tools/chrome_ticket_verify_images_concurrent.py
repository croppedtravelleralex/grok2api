#!/usr/bin/env python3
"""Concurrent image generation with mixed zh/en prompts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from chrome_ticket_experiment_lib import (  # noqa: E402
    log_event,
    panda_image_concurrent_mixed,
    pool_stats,
)

DEFAULT_MIXED_PROMPTS = [
    "一只红苹果放在白色桌面上，产品摄影",
    "a red ceramic mug on white background, product photo",
    "一只橘猫坐在窗台上，自然光",
    "blue cube on white surface, studio lighting",
    "红色公路自行车，侧视图，干净背景",
    "silver wristwatch on marble surface, macro product photo",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Concurrent mixed-prompt image verify")
    parser.add_argument("-n", "--count", type=int, default=6, help="concurrent requests")
    parser.add_argument("-j", "--workers", type=int, default=6, help="thread pool size on Panda")
    parser.add_argument(
        "--prompts-file",
        type=Path,
        help="optional JSON file with prompts list; uses first n prompts",
    )
    args = parser.parse_args()
    n = max(1, int(args.count))
    prompts = list(DEFAULT_MIXED_PROMPTS)
    if args.prompts_file and args.prompts_file.is_file():
        data = json.loads(args.prompts_file.read_text(encoding="utf-8"))
        prompts = data if isinstance(data, list) else data.get("prompts") or prompts
    prompts = [str(p) for p in prompts[:n]]
    while len(prompts) < n:
        prompts.append(DEFAULT_MIXED_PROMPTS[len(prompts) % len(DEFAULT_MIXED_PROMPTS)])

    print("pool_before", json.dumps(pool_stats().get("ByStatus") or pool_stats().get("byStatus")))
    log_event("concurrent_image_mixed_start", count=n, workers=args.workers, prompts=prompts)
    result = panda_image_concurrent_mixed(prompts, workers=min(args.workers, n))
    summary = {
        "target": n,
        "ok": result.get("ok"),
        "pool_hits": result.get("pool_hits"),
        "asset403": result.get("asset403"),
        "results": result.get("results"),
        "pool_after": pool_stats().get("ByStatus") or pool_stats().get("byStatus"),
    }
    log_event(
        "concurrent_image_mixed_done",
        ok=summary["ok"],
        pool_hits=summary["pool_hits"],
        asset403=summary["asset403"],
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if int(summary.get("ok") or 0) >= n else 1


if __name__ == "__main__":
    raise SystemExit(main())
