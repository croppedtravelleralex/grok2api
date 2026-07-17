#!/usr/bin/env python3
"""Verify revived capable accounts are in the production pool."""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from pathlib import Path

IDS = [106, 124, 172, 177, 185, 416, 422, 429, 436, 453, 460, 470, 483, 486, 488, 506, 528, 549]
BASE = "http://127.0.0.1:18000/api/admin/v1"
PASSWORD = Path("/opt/grok2api/staging/admin-password").read_text(encoding="utf-8").strip()
DB = "/opt/grok2api/data/backend.db"


def call(method: str, path: str, token: str = "", payload=None, timeout: int = 90):
    body = None if payload is None else json.dumps(payload).encode()
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


def unwrap(value):
    return value.get("data", value) if isinstance(value, dict) else value


def main() -> None:
    status, login = call("POST", "/auth/login", payload={"username": "admin", "password": PASSWORD})
    token = unwrap(login)["tokens"]["accessToken"]
    print(f"login={status}")

    status, body = call("POST", f"/accounts/{IDS[0]}/refresh-token", token=token, timeout=120)
    data = unwrap(body)
    print(
        "smoke",
        IDS[0],
        "http",
        status,
        "enabled",
        data.get("enabled"),
        "auth",
        data.get("authStatus"),
        "model",
        data.get("observedModel"),
        "err",
        repr(data.get("lastError")),
    )

    ok = 0
    for account_id in IDS:
        _, body = call("GET", f"/accounts/{account_id}", token=token)
        data = unwrap(body)
        if data.get("enabled") and data.get("authStatus") == "active" and data.get("observedModel"):
            ok += 1
        else:
            print(
                "BAD",
                account_id,
                data.get("enabled"),
                data.get("authStatus"),
                data.get("observedModel"),
                data.get("lastError"),
            )
    print(f"api_ok={ok}/{len(IDS)}")

    con = sqlite3.connect(DB)
    prod = con.execute(
        "SELECT COUNT(*) FROM provider_accounts WHERE provider='grok_build' "
        "AND enabled=1 AND auth_status='active' "
        "AND TRIM(COALESCE(observed_model,''))!='' "
        "AND (cooldown_until IS NULL OR cooldown_until <= datetime('now'))"
    ).fetchone()[0]
    placeholders = ",".join("?" * len(IDS))
    revived = con.execute(
        f"SELECT COUNT(*) FROM provider_accounts WHERE id IN ({placeholders}) "
        "AND enabled=1 AND auth_status='active' AND TRIM(COALESCE(observed_model,''))!=''",
        IDS,
    ).fetchone()[0]
    con.close()
    print(f"production_pool={prod}")
    print(f"revived_in_pool={revived}")


if __name__ == "__main__":
    main()
