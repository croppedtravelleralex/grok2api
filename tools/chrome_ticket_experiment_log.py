#!/usr/bin/env python3
"""Append or list Chrome ticket experiment JSONL records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from chrome_ticket_experiment_lib import DEFAULT_LOG, append_experiment_log, log_event


def cmd_append(args: argparse.Namespace) -> int:
    extra: dict = {}
    if args.json:
        extra.update(json.loads(args.json))
    for item in args.field or []:
        if "=" not in item:
            raise SystemExit(f"invalid --field {item!r}, want key=value")
        key, value = item.split("=", 1)
        extra[key] = value
    row = append_experiment_log(args.batch, log_path=Path(args.log), **extra)
    print(json.dumps(row, ensure_ascii=False))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    path = Path(args.log)
    if not path.exists():
        log_event("log_empty", path=str(path))
        return 0
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if args.batch and row.get("batch") != args.batch:
            continue
        rows.append(row)
    if args.last:
        rows = rows[-args.last :]
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    log_event("log_list", count=len(rows), path=str(path))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Chrome ticket experiment JSONL log")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="JSONL path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    append_p = sub.add_parser("append", help="append one record")
    append_p.add_argument("batch", help="batch id, e.g. S0, D1, M2")
    append_p.add_argument("--json", help="extra fields as JSON object")
    append_p.add_argument("--field", action="append", help="key=value field")
    append_p.set_defaults(func=cmd_append)

    list_p = sub.add_parser("list", help="list records")
    list_p.add_argument("--batch", help="filter by batch id")
    list_p.add_argument("--last", type=int, default=0, help="show only last N")
    list_p.set_defaults(func=cmd_list)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
