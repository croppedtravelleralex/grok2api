#!/usr/bin/env python3
"""Serial image generation to verify ticket pool."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from chrome_ticket_experiment_lib import (  # noqa: E402
    parse_image_logs_since,
    panda_image_once,
    pool_stats,
    run_remote_python,
)


def last_image_account_id() -> int | None:
    script = r"""
import json, re, subprocess
logs = subprocess.run(["docker", "logs", "grok2api", "--since", "90s"], capture_output=True, text=True)
text = logs.stdout or ""
aid = None
for line in reversed(text.splitlines()):
    if "account_id" not in line:
        continue
    m = re.search(r'"account_id"\s*:\s*(\d+)', line)
    if m:
        aid = int(m.group(1))
        break
print(json.dumps({"account_id": aid}))
"""
    out = run_remote_python(script, remote_name="_verify_last_account.py", timeout=30)
    payload = json.loads(out.splitlines()[-1])
    aid = payload.get("account_id")
    return int(aid) if aid else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=6)
    parser.add_argument("--gap", type=float, default=2.0, help="seconds between serial requests")
    args = parser.parse_args()
    print("pool_before", json.dumps(pool_stats().get("ByStatus") or pool_stats().get("byStatus")))
    rows: list[dict] = []
    for i in range(1, args.n + 1):
        t0 = time.time()
        img = panda_image_once(log_since_s=45)
        img["seq"] = i
        img["wall_s"] = round(time.time() - t0, 1)
        img["account_id"] = last_image_account_id()
        img["logs"] = parse_image_logs_since(since_s=120)
        rows.append(img)
        diag = img["logs"]
        scopes = [f.get("scope") for f in diag.get("asset_fails") or [] if f.get("status_code") == 403]
        print(
            f"[{i}/{args.n}] http={img.get('http')} pool_hit={img.get('pool_hit')} "
            f"asset403={diag.get('asset403')} scopes403={scopes} "
            f"aid={img.get('account_id')} wall={img.get('wall_s')}s "
            f"url={(img.get('url') or '')[:80]}"
        )
        if args.gap > 0 and i < args.n:
            time.sleep(args.gap)
    summary = {
        "ok": sum(1 for r in rows if r.get("http") == 200),
        "pool_hit": sum(1 for r in rows if r.get("pool_hit")),
        "asset403_total": sum(int((r.get("logs") or {}).get("asset403") or 0) for r in rows),
        "failed_with_403": [
            r["seq"]
            for r in rows
            if r.get("http") != 200 and int((r.get("logs") or {}).get("asset403") or 0) > 0
        ],
        "accounts": dict(Counter(r["account_id"] for r in rows if r.get("account_id"))),
        "rows": rows,
        "pool_after": pool_stats().get("ByStatus") or pool_stats().get("byStatus"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] >= args.n else 1


if __name__ == "__main__":
    raise SystemExit(main())
