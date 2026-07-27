#!/usr/bin/env python3
"""在 Panda 应用 20 并发 profile（settings API + 需重启容器生效槽位/egress 闸门）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("GROK2API_BASE", "http://127.0.0.1:18000").rstrip("/")

WEB20 = {
    "webConcurrency": 20,
    "assetConcurrency": 20,
    "expandConcurrency": 4,
    "promptSlots": 20,
    "sseSlots": 20,
    "mediaConcurrency": 4,
}


def load_password() -> str:
    path = Path("/root/.secrets/grok2api-admin-password")
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    raise SystemExit("missing /root/.secrets/grok2api-admin-password")


def request(method: str, path: str, token: str | None = None, body: dict | None = None) -> dict:
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode())
        return payload.get("data", payload)


def main() -> None:
    restart = "--restart" in sys.argv
    login = request(
        "POST",
        "/api/admin/v1/auth/login",
        body={"username": "admin", "password": load_password()},
    )
    token = login["accessToken"]
    snap = request("GET", "/api/admin/v1/settings", token)
    cfg = snap["config"]
    cfg["providerWeb"].update(WEB20)
    result = request("PATCH", "/api/admin/v1/settings", token, {"revision": snap["revision"], "config": cfg})
    print(json.dumps({"restartRequired": result.get("restartRequired"), "web20": WEB20}, indent=2))
    if restart:
        subprocess.run(["docker", "compose", "-f", "/opt/grok2api/docker-compose.yml", "up", "-d", "grok2api"], check=True)
        print("container restarted")


if __name__ == "__main__":
    main()
