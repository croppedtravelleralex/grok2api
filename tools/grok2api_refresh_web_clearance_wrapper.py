#!/usr/bin/env python3
"""刷新 grok_web 出口 Cloudflare clearance（需 FlareSolverr）。

若 FlareSolverr 未运行则 exit 0 并打印 skip，避免 systemd timer 反复 failed。
部署：Panda 上 `docker run -d --name flaresolverr --restart unless-stopped -p 127.0.0.1:18191:8191 ghcr.io/flaresolverr/flaresolverr:latest`
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

FLARESOLVERR = os.environ.get("FLARESOLVERR", "http://127.0.0.1:18191/v1")


def flare_available() -> bool:
    try:
        body = json.dumps({"cmd": "sessions.create"}).encode()
        req = urllib.request.Request(
            FLARESOLVERR.rstrip("/"),
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
            return payload.get("status") == "ok"
    except Exception:
        return False


def main() -> int:
    if not flare_available():
        print(f"skip: flaresolverr unavailable at {FLARESOLVERR}")
        return 0
    # 完整刷新逻辑在 Panda 宿主机版；此处仅提供健康检查包装。
    # 若需完整实现，从 /usr/local/sbin/grok2api-refresh-web-clearance.py 迁入本仓库。
    legacy = "/usr/local/sbin/grok2api-refresh-web-clearance.py"
    if os.path.isfile(legacy):
        import subprocess

        env = os.environ.copy()
        env["FLARESOLVERR"] = FLARESOLVERR
        return subprocess.call([sys.executable, legacy], env=env)
    print("flare ok but legacy refresh script missing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
