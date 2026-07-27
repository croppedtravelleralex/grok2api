#!/usr/bin/env python3
"""一次性关闭 Panda 上陈旧的 image_pipeline running trace（运维脚本）。"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB = Path("/opt/grok2api/data/backend.db")
MINUTES = int(__import__("os").environ.get("STALE_MINUTES", "30"))


def main() -> int:
    if not DB.exists():
        print(f"database missing: {DB}", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*) FROM image_pipeline_traces
        WHERE status='running' AND ended_at IS NULL
          AND started_at < datetime('now', ?)
        """,
        (f"-{MINUTES} minutes",),
    )
    count = cur.fetchone()[0]
    if count == 0:
        print("no stale running traces")
        return 0
    ended = now.isoformat()
    cur.execute(
        """
        UPDATE image_pipeline_traces
        SET status='failed', error_code='stale_abandoned', ended_at=?
        WHERE status='running' AND ended_at IS NULL
          AND started_at < datetime('now', ?)
        """,
        (ended, f"-{MINUTES} minutes"),
    )
    cur.execute(
        """
        UPDATE image_pipeline_traces
        SET total_ms = CAST((julianday(ended_at) - julianday(started_at)) * 86400000 AS INTEGER)
        WHERE status='failed' AND error_code='stale_abandoned' AND ended_at = ?
        """,
        (ended,),
    )
    conn.commit()
    print(f"closed_stale_running={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
