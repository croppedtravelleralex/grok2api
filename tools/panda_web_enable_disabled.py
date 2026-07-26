#!/usr/bin/env python3
"""Batch-enable disabled grok_web accounts so they enter probe / dispatch indexing."""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

BASE = os.environ.get("GROK2API_BASE", "http://127.0.0.1:18000").rstrip("/")
CHUNK = int(os.environ.get("GROK2API_ENABLE_CHUNK", "100"))


def load_password() -> str:
    for path in (Path("/root/.secrets/grok2api-admin-password"),):
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    raise SystemExit("admin password missing")


def api(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=300) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return resp.status, json.loads(raw) if raw else {}


def login() -> str:
    password = load_password()
    status, payload = api("POST", "/api/admin/v1/auth/login", "", {"username": "admin", "password": password})
    if status != 200:
        raise SystemExit(f"login failed status={status}")
    return payload["data"]["tokens"]["accessToken"]


def list_disabled_ids(token: str) -> list[str]:
    ids: list[str] = []
    page = 1
    while True:
        status, payload = api(
            "GET",
            f"/api/admin/v1/accounts?provider=grok_web&status=disabled&page={page}&pageSize=100",
            token,
        )
        if status != 200:
            raise SystemExit(f"list failed page={page} status={status}")
        data = payload.get("data") or {}
        items = data.get("items") or []
        for item in items:
            ids.append(str(item["id"]))
        total = int(data.get("total") or 0)
        if page * 100 >= total or not items:
            break
        page += 1
    return ids


def batch_enable(token: str, ids: list[str]) -> int:
    updated = 0
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i : i + CHUNK]
        status, payload = api(
            "PATCH",
            "/api/admin/v1/accounts/batch",
            token,
            {"ids": chunk, "provider": "grok_web", "enabled": True},
        )
        if status != 200:
            raise SystemExit(f"batch enable failed chunk={i // CHUNK + 1} status={status} body={payload}")
        updated += int((payload.get("data") or {}).get("updated") or 0)
        print(f"chunk {i // CHUNK + 1}: enabled {len(chunk)} updated={payload.get('data')}", flush=True)
    return updated


def probe_snapshot(token: str) -> dict:
    status, payload = api("GET", "/api/admin/v1/accounts/web-probe", token)
    if status != 200:
        return {}
    data = payload.get("data") or {}
    return {
        "pools": data.get("pools"),
        "budget": {
            k: (data.get("budget") or {}).get(k)
            for k in ("liteGlobalPerHour", "liteGlobalUsedHour", "pipelineLoadPercent")
        },
    }


def main() -> None:
    token = login()
    before = probe_snapshot(token)
    ids = list_disabled_ids(token)
    print(f"disabled_ids={len(ids)}", flush=True)
    if not ids:
        print(json.dumps({"updated": 0, "before": before, "after": before}, ensure_ascii=False, indent=2))
        return
    updated = batch_enable(token, ids)
    after = probe_snapshot(token)
    summary = {"updated": updated, "disabled_before": len(ids), "before": before, "after": after}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
