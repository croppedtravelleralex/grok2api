#!/usr/bin/env python3
"""Chrome ticket pool probe — prefers Rust unified IO when binary is available."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from chrome_ticket_experiment_lib import log_event, ticket_pool_probe  # noqa: E402

RUST_BIN = TOOLS / "chrome_ticket_probe_rs" / "target" / "release" / "chrome_ticket_probe.exe"
RUST_BIN_UNIX = TOOLS / "chrome_ticket_probe_rs" / "target" / "release" / "chrome_ticket_probe"


def rust_probe_path() -> Path | None:
    if RUST_BIN.is_file():
        return RUST_BIN
    if RUST_BIN_UNIX.is_file():
        return RUST_BIN_UNIX
    return None


def run_rust_probe(concurrent: int, tickets: int, workers: int | None, quiet: bool) -> int:
    bin_path = rust_probe_path()
    if not bin_path:
        raise FileNotFoundError("rust probe binary missing")
    cmd = [str(bin_path)]
    if workers is not None:
        cmd.extend(["--workers", str(workers)])
    cmd.extend(
        [
            "probe",
            "-c",
            str(concurrent),
            "-n",
            str(tickets),
        ]
    )
    if quiet:
        cmd.append("--quiet")
    proc = subprocess.run(cmd, text=True, capture_output=not quiet)
    if not quiet and proc.stdout:
        print(proc.stdout, end="")
    if quiet and proc.stdout:
        print(proc.stdout.strip())
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Chrome ticket pool probe")
    parser.add_argument("-c", "--concurrent", type=int, default=5, help="target concurrent image slots")
    parser.add_argument("-n", "--tickets", type=int, default=20, help="target available tickets")
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=None,
        help="concurrent quota fetch workers on Panda",
    )
    parser.add_argument("--python-only", action="store_true", help="force Python probe (no Rust)")
    parser.add_argument("--quiet", action="store_true", help="only print readiness summary line")
    parser.add_argument(
        "--remediate",
        action="store_true",
        help="purge orphan tickets + pin sync before probe",
    )
    args = parser.parse_args()

    if args.remediate:
        from chrome_ticket_experiment_lib import remediate_pool_probe

        report = remediate_pool_probe(
            target_concurrent=args.concurrent,
            target_tickets=args.tickets,
            workers=args.workers,
        )
        log_event(
            "ticket_pool_probe",
            ready_both=report["readiness"]["ready_for_both"],
            blockers=report["readiness"]["blockers"],
            available=report["ticket_pool"]["available_total"],
            remediated=True,
        )
        if args.quiet:
            print(
                json.dumps(
                    {
                        "ready_for_both": report["readiness"]["ready_for_both"],
                        "blockers": report["readiness"]["blockers"],
                        "available_total": report["ticket_pool"]["available_total"],
                        "remediation": report.get("remediation"),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["readiness"]["ready_for_both"] else 1

    if not args.python_only and rust_probe_path() and not args.remediate:
        try:
            return run_rust_probe(args.concurrent, args.tickets, args.workers, args.quiet)
        except Exception as exc:
            log_event("rust_probe_fallback", error=str(exc)[:200])

    report = ticket_pool_probe(
        target_concurrent=args.concurrent,
        target_tickets=args.tickets,
        workers=args.workers,
    )
    log_event(
        "ticket_pool_probe",
        ready_both=report["readiness"]["ready_for_both"],
        blockers=report["readiness"]["blockers"],
        available=report["ticket_pool"]["available_total"],
        runtime_ticket_accounts=len([r for r in report["ticket_pool"]["ticket_holders"] if r.get("in_runtime")]),
        io_mode=report.get("io_mode"),
    )
    if args.quiet:
        print(
            json.dumps(
                {
                    "ready_for_both": report["readiness"]["ready_for_both"],
                    "blockers": report["readiness"]["blockers"],
                    "available_total": report["ticket_pool"]["available_total"],
                },
                ensure_ascii=False,
            )
        )
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["readiness"]["ready_for_both"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
