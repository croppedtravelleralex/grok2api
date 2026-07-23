#!/usr/bin/env python3
"""Chrome ticket pool on Panda (stores statsig_meta + device cookies, not short-lived statsig).

DB default: /opt/grok2api/data/chrome_ticket_pool.db
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("CHROME_TICKET_POOL_DB", "/opt/grok2api/data/chrome_ticket_pool.db"))
DEFAULT_TTL_HOURS = float(os.environ.get("CHROME_TICKET_TTL_HOURS", "12"))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chrome_tickets (
            id TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL,
            statsig_meta TEXT NOT NULL,
            device_cookie TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT '',
            sign_source TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            status TEXT NOT NULL DEFAULT 'available'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chrome_tickets_avail ON chrome_tickets(status, expires_at, account_id)"
    )
    conn.commit()
    return conn


def push_ticket(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    statsig_meta: str,
    device_cookie: str = "",
    user_agent: str = "",
    sign_source: str = "",
    ttl_hours: float = DEFAULT_TTL_HOURS,
) -> str:
    meta = (statsig_meta or "").strip()
    if not meta:
        raise ValueError("statsig_meta required")
    now = utcnow()
    tid = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO chrome_tickets (
            id, account_id, statsig_meta, device_cookie, user_agent, sign_source,
            created_at, expires_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'available')
        """,
        (
            tid,
            account_id,
            meta,
            device_cookie or "",
            user_agent or "",
            sign_source or "",
            iso(now),
            iso(now + timedelta(hours=ttl_hours)),
        ),
    )
    conn.commit()
    return tid


def sweep(conn: sqlite3.Connection) -> int:
    now = iso(utcnow())
    cur = conn.execute(
        """
        UPDATE chrome_tickets
        SET status='expired'
        WHERE status='available' AND expires_at < ?
        """,
        (now,),
    )
    conn.commit()
    return cur.rowcount


def stats(conn: sqlite3.Connection) -> dict:
    sweep(conn)
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS n
        FROM chrome_tickets
        GROUP BY status
        """
    ).fetchall()
    by_account = conn.execute(
        """
        SELECT account_id, COUNT(*) AS n
        FROM chrome_tickets
        WHERE status='available'
        GROUP BY account_id
        ORDER BY n DESC
        LIMIT 20
        """
    ).fetchall()
    return {
        "by_status": {r["status"]: r["n"] for r in rows},
        "available_by_account": [(r["account_id"], r["n"]) for r in by_account],
    }


def pop(conn: sqlite3.Connection, account_id: int | None = None) -> dict | None:
    sweep(conn)
    now = iso(utcnow())
    if account_id is not None:
        row = conn.execute(
            """
            SELECT * FROM chrome_tickets
            WHERE status='available' AND expires_at >= ? AND account_id=?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (now, account_id),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT * FROM chrome_tickets
            WHERE status='available' AND expires_at >= ?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()
    if not row:
        return None
    conn.execute(
        "UPDATE chrome_tickets SET status='consumed', consumed_at=? WHERE id=?",
        (iso(utcnow()), row["id"]),
    )
    conn.commit()
    return dict(row)


def ticket_to_lite_payload(row: dict, prompt: str) -> dict:
    return {
        "version": 2,
        "account_id": int(row["account_id"]),
        "statsig": "",
        "statsig_meta": row["statsig_meta"],
        "cookie": row["device_cookie"] or "",
        "user_agent": row["user_agent"] or "",
        "prompt": prompt,
        "signed_at": row["created_at"],
        "sign_source": row.get("sign_source") or "pool",
        "pool_id": row["id"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Chrome ticket pool on Panda")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_push = sub.add_parser("push", help="push ticket JSON from stdin")
    p_push.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS)

    sub.add_parser("pop", help="pop one ticket JSON to stdout")
    sub.add_parser("sweep", help="mark expired tickets")
    sub.add_parser("stats", help="pool statistics")

    args = parser.parse_args()
    conn = connect(args.db)

    if args.cmd == "push":
        raw = json.loads(sys.stdin.read())
        tid = push_ticket(
            conn,
            account_id=int(raw["account_id"]),
            statsig_meta=str(raw.get("statsig_meta") or raw.get("statsigMeta") or ""),
            device_cookie=str(raw.get("cookie") or raw.get("device_cookie") or ""),
            user_agent=str(raw.get("user_agent") or ""),
            sign_source=str(raw.get("sign_source") or ""),
            ttl_hours=args.ttl_hours,
        )
        print(json.dumps({"ok": True, "id": tid}, ensure_ascii=False))
    elif args.cmd == "pop":
        row = pop(conn)
        if not row:
            raise SystemExit(1)
        print(json.dumps(row, ensure_ascii=False))
    elif args.cmd == "sweep":
        n = sweep(conn)
        print(json.dumps({"expired": n}, ensure_ascii=False))
    elif args.cmd == "stats":
        print(json.dumps(stats(conn), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
