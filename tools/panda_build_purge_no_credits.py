#!/usr/bin/env python3
"""Mark grok_build no-credit / stuck-verification accounts deletable and delete them."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("GROK2API_BASE", "http://127.0.0.1:18000").rstrip("/")
DB = Path(os.environ.get("GROK2API_DB", "/opt/grok2api/data/backend.db"))
OUT = Path(os.environ.get("GROK2API_OUT", "/opt/grok2api/data/build-purge"))


def load_password() -> str:
    for path in (Path("/root/.secrets/grok2api-admin-password"), Path("/opt/grok2api/staging/admin-password")):
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    raise SystemExit("admin password missing")


def api(method: str, path: str, token: str = "", body: dict | None = None) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {"error": raw}
        except json.JSONDecodeError:
            payload = {"error": raw}
        return exc.code, payload


def login(password: str) -> str:
    status, payload = api("POST", "/api/admin/v1/auth/login", body={"username": "admin", "password": password})
    if status != 200:
        raise SystemExit(f"login failed: {status} {payload}")
    return payload["data"]["tokens"]["accessToken"]


def candidate_ids() -> list[int]:
    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT id FROM provider_accounts
        WHERE provider='grok_build'
          AND (
            enabled=0 AND lower(COALESCE(last_error,'')) LIKE 'deletable:%'
            OR (
              enabled=1 AND (
                observed_model IS NULL OR trim(observed_model)=''
              )
            )
            OR lower(COALESCE(last_error,'')) LIKE '%spending-limit%'
            OR lower(COALESCE(last_error,'')) LIKE '%run out of credits%'
            OR lower(COALESCE(last_error,'')) LIKE '%402%'
          )
        ORDER BY id
        """
    ).fetchall()
    conn.close()
    return [int(r[0]) for r in rows]


def mark_deletable(ids: list[int], reason: str) -> int:
    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()
    marked = 0
    for account_id in ids:
        cur.execute(
            """
            UPDATE provider_accounts
            SET enabled=0,
                auth_status='reauthRequired',
                last_error=?
            WHERE id=? AND provider='grok_build'
            """,
            (f"deletable: {reason}", account_id),
        )
        marked += cur.rowcount
    conn.commit()
    conn.close()
    return marked


def batch_delete(token: str, ids: list[int]) -> int:
    deleted = 0
    for start in range(0, len(ids), 100):
        chunk = [str(i) for i in ids[start : start + 100]]
        status, payload = api("DELETE", "/api/admin/v1/accounts", token=token, body={"provider": "grok_build", "ids": chunk})
        if status != 200:
            raise SystemExit(f"delete failed status={status} payload={payload}")
        deleted += int(payload.get("data", {}).get("deleted", len(chunk)))
    return deleted


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    password = load_password()
    token = login(password)

    # Enable purge apply so delete probe can work if needed later
    api("PATCH", "/api/admin/v1/accounts/build-probe", token=token, body={"purgeApply": True})

    ids = candidate_ids()
    print(f"candidates={len(ids)}", flush=True)
    marked = mark_deletable(ids, "build no credits / verification 402")
    print(f"marked_deletable={marked}", flush=True)

    # Re-fetch deletable ids
    conn = sqlite3.connect(str(DB))
    del_ids = [
        int(r[0])
        for r in conn.execute(
            "SELECT id FROM provider_accounts WHERE provider='grok_build' AND lower(COALESCE(last_error,'')) LIKE 'deletable:%'"
        ).fetchall()
    ]
    conn.close()

    deleted = batch_delete(token, del_ids) if del_ids else 0
    summary = {
        "stamp": stamp,
        "candidates": ids,
        "marked": marked,
        "deleted_ids": del_ids,
        "deleted": deleted,
    }
    out = OUT / f"build-purge-{stamp}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"summary={out}")


if __name__ == "__main__":
    main()
