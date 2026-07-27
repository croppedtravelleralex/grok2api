#!/usr/bin/env python3
"""Trigger full Grok Web quota sync on panda."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

BASE = os.environ.get("GROK2API_ADMIN_BASE", "http://127.0.0.1:18000/api/admin/v1").rstrip("/")


def load_password() -> str:
    env = os.environ.get("GROK2API_ADMIN_PASSWORD", "").strip()
    if env:
        return env
    path = Path(os.environ.get("GROK2API_ADMIN_PASSWORD_FILE", "/root/.secrets/grok2api-admin-password"))
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    raise SystemExit("admin password missing")


def main() -> None:
    pw = load_password()
    login = urllib.request.Request(
        f"{BASE}/auth/login",
        data=json.dumps({"username": "admin", "password": pw}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(login, timeout=60) as resp:
        token = json.load(resp)["data"]["tokens"]["accessToken"]

    req = urllib.request.Request(
        f"{BASE}/accounts/web/refresh-quotas",
        data=b"",
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        method="POST",
    )
    print("triggering /accounts/web/refresh-quotas ...", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=7200) as resp:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                for line in text.splitlines():
                    if line.startswith("data:"):
                        print(line[5:].strip(), flush=True)
    except Exception as exc:
        print(f"sync stream ended: {exc}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
