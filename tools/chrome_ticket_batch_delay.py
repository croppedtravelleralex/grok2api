#!/usr/bin/env python3
"""Batch D wrapper: mint → wait → single consume."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Chrome ticket delay batch (D0/D1/D2)")
    parser.add_argument("--wait", required=True, help="delay before consume: 0, 5m, 30m")
    parser.add_argument("--batch", help="batch label override")
    parser.add_argument("--account-id", type=int, default=1467)
    parser.add_argument("--sso-file", default=str(ROOT / ".tmp" / "web-sso-canary-1467.json"))
    args, extra = parser.parse_known_args()
    cmd = [
        sys.executable,
        str(ROOT / "tools" / "chrome_ticket_batch_run.py"),
        "delay",
        "--wait",
        args.wait,
        "--account-id",
        str(args.account_id),
        "--sso-file",
        args.sso_file,
        *extra,
    ]
    if args.batch:
        cmd.extend(["--batch", args.batch])
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
