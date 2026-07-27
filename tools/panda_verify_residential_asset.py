#!/usr/bin/env python3
"""验证住宅 asset 节点：临时禁用 udeal-111，单次生图，再恢复。"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("GROK2API_BASE", "http://127.0.0.1:18000").rstrip("/")
NODE_ID = int(os.environ.get("GROK2API_UDEAL_ASSET_NODE_ID", "111"))


def load_password() -> str:
    return Path("/root/.secrets/grok2api-admin-password").read_text(encoding="utf-8").strip()


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode()).get("data", {})


def find_node(token: str, node_id: int) -> dict:
    payload = api("GET", "/api/admin/v1/egress-nodes?scope=grok_web_asset", token)
    nodes = payload.get("items", payload)
    for node in nodes:
        if int(node["id"]) == node_id:
            return node
    raise SystemExit(f"node {node_id} not found")


def set_enabled(token: str, node: dict, enabled: bool) -> None:
    api(
        "PUT",
        f"/api/admin/v1/egress-nodes/{node['id']}",
        token,
        {
            "name": node["name"],
            "scope": node["scope"],
            "enabled": enabled,
            "proxyURL": node.get("proxyURL") or "",
            "userAgent": node.get("userAgent") or "",
        },
    )


def one_image(token: str) -> dict:
    key = os.environ.get("GROK2API_KEY", "").strip()
    if not key:
        raise SystemExit("set GROK2API_KEY")
    body = json.dumps({"model": "grok-imagine-image", "prompt": "a red mug on white table", "n": 1}).encode()
    req = urllib.request.Request(
        f"{BASE}/v1/images/generations",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode())
            url = (payload.get("data") or [{}])[0].get("url")
            return {"ok": resp.status == 200 and bool(url), "status": resp.status, "url": url}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": exc.read().decode()[:400]}


def main() -> None:
    token = api("POST", "/api/admin/v1/auth/login", "", {"username": "admin", "password": load_password()})["tokens"]["accessToken"]
    node = find_node(token, NODE_ID)
    was_enabled = bool(node.get("enabled"))
    report = {"node": node["name"], "was_enabled": was_enabled}
    try:
        if was_enabled:
            set_enabled(token, node, False)
        report["without_udeal"] = one_image(token)
    finally:
        if was_enabled:
            set_enabled(token, node, True)
        report["restored"] = was_enabled
    print(json.dumps(report, indent=2))
    if not report.get("without_udeal", {}).get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
