#!/usr/bin/env python3
"""Run survival tiers sequentially; stop on first failure. Long tiers: mint now, consume later."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

TIERS_SHORT = ["10m", "15m", "30m", "60m"]
TIERS_LONG = ["3h", "6h", "12h"]
ALL_TIERS = TIERS_SHORT + TIERS_LONG

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools" / "chrome_ticket_survival_runner.py"


def run_one(age: str, account_id: int, sso: str, *, phase: str = "full") -> int:
    cmd = [
        sys.executable,
        str(RUNNER),
        "--age",
        age,
        "--account-id",
        str(account_id),
        "--sso-file",
        sso,
        "--phase",
        phase,
        "--stop-on-fail",
    ]
    print(f"\n=== survival tier {age} phase={phase} account={account_id} ===\n", flush=True)
    return subprocess.call(cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sequential survival matrix")
    parser.add_argument("--only", help="comma ages e.g. 10m,15m")
    parser.add_argument("--account-id", type=int, default=1467)
    parser.add_argument("--sso-file", default=str(ROOT / ".tmp" / "web-sso-canary-1467.json"))
    parser.add_argument("--long-mint-only", action="store_true", help="for 3h+ only mint and save state")
    args = parser.parse_args()

    tiers = [x.strip() for x in (args.only or ",".join(ALL_TIERS)).split(",") if x.strip()]
    for age in tiers:
        phase = "full"
        if args.long_mint_only and age in TIERS_LONG:
            phase = "mint"
        rc = run_one(age, args.account_id, args.sso_file, phase=phase)
        if rc != 0:
            print(f"STOP: tier {age} failed rc={rc}", flush=True)
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
