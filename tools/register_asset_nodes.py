#!/usr/bin/env python3
"""幂等注册 Webshare 代理为 grok_web_asset egress 节点。

文件格式：host:port:user:pass（每行一条）
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def load_password() -> str:
    for path in (
        Path("/root/.secrets/grok2api-admin-password"),
        Path(os.environ.get("GROK2API_ADMIN_PASSWORD_FILE", "")),
    ):
        if path and path.exists():
            return path.read_text(encoding="utf-8").strip()
    raise SystemExit("admin password missing (set GROK2API_ADMIN_PASSWORD_FILE)")


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode())
        return payload.get("data", payload)


def parse_proxy_line(line: str) -> str:
    parts = line.strip().split(":")
    if len(parts) < 4:
        raise ValueError(f"bad proxy line: {line[:80]}")
    host, port, user, password = parts[0], parts[1], parts[2], ":".join(parts[3:])
    return f"http://{user}:{password}@{host}:{port}"


def main() -> None:
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="proxy list host:port:user:pass")
    parser.add_argument("--prefix", default="wsres-asset", help="node name prefix")
    parser.add_argument("--base", default=os.environ.get("GROK2API_BASE", "http://127.0.0.1:18000"))
    args = parser.parse_args()
    BASE = args.base.rstrip("/")

    login = api(
        "POST",
        "/api/admin/v1/auth/login",
        "",
        {"username": os.environ.get("GROK2API_ADMIN_USER", "admin"), "password": load_password()},
    )
    token = login["accessToken"]
    existing = {n["name"]: n for n in api("GET", "/api/admin/v1/egress-nodes?scope=grok_web_asset", token)}
    lines = [ln.strip() for ln in Path(args.file).read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]

    created, skipped, failed = 0, 0, 0
    for index, line in enumerate(lines, start=1):
        name = f"{args.prefix}-{index:03d}"
        if name in existing:
            skipped += 1
            continue
        try:
            proxy_url = parse_proxy_line(line)
            api(
                "POST",
                "/api/admin/v1/egress-nodes",
                token,
                {"name": name, "scope": "grok_web_asset", "enabled": True, "proxyURL": proxy_url},
            )
            created += 1
        except (urllib.error.HTTPError, ValueError) as exc:
            failed += 1
            print(f"FAILED {name}: {exc}")

    print(f"APPLIED: created={created} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
