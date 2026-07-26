#!/usr/bin/env python3
"""On Panda: list image dispatch pool account IDs (WebPoolDispatch)."""
from __future__ import annotations

import json
import subprocess
import urllib.request
from datetime import datetime, timezone

BASE = "http://127.0.0.1:18000"
FRESH_SEC = 30 * 60
UPSTREAM = "grok-imagine-image"


def token() -> str:
    return subprocess.check_output(["python3", "/tmp/_panda_admin_token.py"], text=True).strip()


def api(path: str, tok: str) -> dict:
    req = urllib.request.Request(f"{BASE}/api/admin/v1{path}", headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode())
    return payload.get("data") or payload


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def imagine_fresh(window: dict | None, now: datetime) -> bool:
    if not window or window.get("mode") != "imagine":
        return False
    if window.get("source") != "upstream":
        return False
    total = int(window.get("total") or 0)
    remaining = int(window.get("remaining") or 0)
    if total <= 0 or remaining <= 0:
        return False
    synced = parse_iso(window.get("syncedAt"))
    if not synced:
        return False
    return (now - synced).total_seconds() <= FRESH_SEC


def model_state(item: dict) -> dict | None:
    for row in item.get("modelStates") or []:
        if row.get("upstreamModel") == UPSTREAM:
            return row
    return None


def dispatch_admissible(item: dict, now: datetime) -> bool:
    if not item.get("enabled") or item.get("authStatus") != "active":
        return False
    cd = parse_iso(item.get("cooldownUntil"))
    if cd and cd > now:
        return False
    window = next((w for w in item.get("quotaWindows") or [] if w.get("mode") == "imagine"), None)
    if not imagine_fresh(window, now):
        return False
    ms = model_state(item)
    if not ms:
        return False
    return (ms.get("status") or "") in ("available", "quota_available")


def main() -> None:
    tok = token()
    pools = api("/accounts/web-pools", tok)
    ids = pools.get("imageDispatchPoolIds") or []
    if ids:
        print(json.dumps({"source": "imageDispatchPoolIds", "ids": sorted(int(x) for x in ids)}))
        return
    now = datetime.now(timezone.utc)
    out: list[int] = []
    page = 1
    while True:
        data = api(f"/accounts?provider=grok_web&page={page}&pageSize=200", tok)
        items = data.get("items") or []
        for item in items:
            aid = int(item.get("id") or 0)
            if aid and dispatch_admissible(item, now):
                out.append(aid)
        if len(items) < 200:
            break
        page += 1
    print(json.dumps({"source": "client_classify", "ids": sorted(out), "fourPools": pools.get("fourPools")}))


if __name__ == "__main__":
    main()
