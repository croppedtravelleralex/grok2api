#!/usr/bin/env python3
"""Revive capable Build accounts into the production pool."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

IDS = [106, 124, 172, 177, 185, 416, 422, 429, 436, 453, 460, 470, 483, 486, 488, 506, 528, 549]
DB = Path("/opt/grok2api/data/backend.db")


def main() -> None:
    if not DB.exists():
        raise SystemExit(f"db missing: {DB}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = DB.with_name(f"backend-before-revive-capable-{stamp}.db")
    if not any(DB.parent.glob("backend-before-revive-capable-*.db")):
        backup.write_bytes(DB.read_bytes())
        print(f"backup={backup.name}")
    else:
        print("backup=existing-ok")

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    placeholders = ",".join("?" * len(IDS))

    before = cur.execute(
        f"SELECT id,enabled,auth_status,last_error,observed_model FROM provider_accounts WHERE id IN ({placeholders}) ORDER BY id",
        IDS,
    ).fetchall()
    print("BEFORE")
    for row in before:
        print(dict(row))

    cur.execute(
        f"""
        UPDATE provider_accounts
        SET enabled=1,
            auth_status='active',
            last_error='',
            failure_count=0,
            cooldown_until=NULL,
            updated_at=datetime('now')
        WHERE id IN ({placeholders}) AND provider='grok_build'
        """,
        IDS,
    )
    print(f"rows_updated={cur.rowcount}")
    conn.commit()

    after = cur.execute(
        f"SELECT id,enabled,auth_status,last_error,observed_model FROM provider_accounts WHERE id IN ({placeholders}) ORDER BY id",
        IDS,
    ).fetchall()
    print("AFTER")
    for row in after:
        print(dict(row))

    queries = {
        "production": (
            "SELECT COUNT(*) FROM provider_accounts "
            "WHERE provider='grok_build' AND enabled=1 AND auth_status='active' "
            "AND TRIM(COALESCE(observed_model,''))!='' "
            "AND (cooldown_until IS NULL OR cooldown_until <= datetime('now'))"
        ),
        "enabled_active_observed": (
            "SELECT COUNT(*) FROM provider_accounts "
            "WHERE provider='grok_build' AND enabled=1 AND auth_status='active' "
            "AND TRIM(COALESCE(observed_model,''))!=''"
        ),
        "disabled": "SELECT COUNT(*) FROM provider_accounts WHERE provider='grok_build' AND enabled=0",
    }
    for label, sql in queries.items():
        print(f"{label}={cur.execute(sql).fetchone()[0]}")
    conn.close()


if __name__ == "__main__":
    main()
