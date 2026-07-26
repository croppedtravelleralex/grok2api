#!/usr/bin/env python3
"""Classify all grok_web accounts: why schedulable vs not."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

BASE = os.environ.get("GROK2API_BASE", "http://127.0.0.1:18000").rstrip("/")
OUT = Path(os.environ.get("GROK2API_OUT", "/opt/grok2api/data/web-pool-breakdown"))


def load_password() -> str:
    for path in (Path("/root/.secrets/grok2api-admin-password"),):
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    raise SystemExit("admin password missing")


def api(method: str, path: str, token: str) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode())["data"]


def classify_account(item: dict) -> dict:
    enabled = bool(item.get("enabled"))
    auth = item.get("authStatus") or ""
    err = (item.get("lastError") or "").lower()
    if not enabled:
        if auth == "reauthRequired" or "deletable:" in err or err.startswith("web_dead:"):
            bucket = "disabled_credential_dead"
        else:
            bucket = "disabled_manual_or_other"
        return {"bucket": bucket, "image_pool": "", "chat_pool": ""}

    if auth == "reauthRequired":
        return {"bucket": "enabled_reauth_required", "image_pool": "dead", "chat_pool": "dead"}

    ms = None
    for s in item.get("modelStates") or []:
        if s.get("upstreamModel") == "grok-imagine-image":
            ms = s
            break
    ms_status = (ms or {}).get("status") or "no_state"
    imagine = None
    for w in item.get("quotaWindows") or []:
        if w.get("mode") == "imagine":
            imagine = w
            break
    fast = auto = 0
    for w in item.get("quotaWindows") or []:
        if w.get("mode") == "fast":
            fast = int(w.get("remaining") or 0)
        if w.get("mode") == "auto":
            auto = int(w.get("remaining") or 0)

    img_rem = int((imagine or {}).get("remaining") or 0)
    img_tot = int((imagine or {}).get("total") or 0)

    # image pool heuristic mirroring WebPoolAt / webLaneInRecovery
    image_pool = "dispatch"
    image_reason = "eligible"
    if err.startswith("web_dead:"):
        image_pool = "dead"
        image_reason = "web_dead_marker"
    elif ms_status in ("auth_failed", "signature_failed"):
        image_pool = "dead"
        image_reason = f"model_{ms_status}"
    elif ms_status == "quota_exhausted" and not (img_tot > 0 and img_rem > 0):
        image_pool = "recovery"
        image_reason = "quota_exhausted_no_positive_window"
    elif ms_status == "soft_stop":
        image_pool = "recovery"
        image_reason = "soft_stop"
    elif ms_status in ("unknown", "quota_available", "no_state") and img_tot == 0 and img_rem == 0:
        image_pool = "recovery"
        image_reason = "imagine_0_0_pending_l2"
    elif img_tot > 0 and img_rem <= 0 and ms_status != "available":
        image_pool = "recovery"
        image_reason = "imagine_window_empty"
    elif ms_status == "available" or (img_tot > 0 and img_rem > 0):
        image_pool = "dispatch"
        image_reason = "ready"

    chat_pool = "dispatch" if (fast > 0 or auto > 0) else "recovery"
    chat_reason = "chat_quota_ok" if chat_pool == "dispatch" else "chat_quota_empty"

    bucket = f"enabled_image_{image_pool}__{image_reason}"
    return {
        "bucket": bucket,
        "image_pool": image_pool,
        "chat_pool": chat_pool,
        "image_reason": image_reason,
        "chat_reason": chat_reason,
        "model_status": ms_status,
        "imagine": f"{img_rem}/{img_tot}",
        "fast": fast,
        "auto": auto,
    }


def login(password: str) -> str:
    req = urllib.request.Request(
        f"{BASE}/api/admin/v1/auth/login",
        data=json.dumps({"username": "admin", "password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())["data"]["tokens"]["accessToken"]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    token = login(load_password())

    probe = api("GET", "/api/admin/v1/accounts/web-probe", token)
    pools = probe.get("pools", {})

    buckets = Counter()
    image_reasons = Counter()
    chat_reasons = Counter()
    disabled_reasons = Counter()
    samples: dict[str, list] = defaultdict(list)
    total = 0
    page = 1
    while True:
        data = api("GET", f"/api/admin/v1/accounts?provider=grok_web&page={page}&pageSize=200", token)
        items = data.get("items", [])
        for item in items:
            total += 1
            info = classify_account(item)
            buckets[info["bucket"]] += 1
            if not item.get("enabled"):
                disabled_reasons[info["bucket"]] += 1
            else:
                image_reasons[info["image_reason"]] += 1
                chat_reasons[info["chat_reason"]] += 1
            if len(samples[info["bucket"]]) < 3:
                samples[info["bucket"]].append({
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "enabled": item.get("enabled"),
                    "auth": item.get("authStatus"),
                    "imagine": info.get("imagine"),
                    "model": info.get("model_status"),
                    "lastError": (item.get("lastError") or "")[:100],
                })
        if len(items) < 200:
            break
        page += 1

    img_dispatch = img_recovery = img_dead = chat_d = chat_r = 0
    disabled = 0
    page = 1
    while True:
        data = api("GET", f"/api/admin/v1/accounts?provider=grok_web&page={page}&pageSize=200", token)
        for item in data.get("items", []):
            info = classify_account(item)
            if not item.get("enabled"):
                disabled += 1
                continue
            if info["image_pool"] == "dispatch":
                img_dispatch += 1
            elif info["image_pool"] == "recovery":
                img_recovery += 1
            else:
                img_dead += 1
            if info["chat_pool"] == "dispatch":
                chat_d += 1
            else:
                chat_r += 1
        if len(data.get("items", [])) < 200:
            break
        page += 1

    enabled = total - disabled
    summary = {
        "stamp": stamp,
        "total_accounts": total,
        "disabled": disabled,
        "enabled": total - disabled,
        "api_three_pools": pools,
        "heuristic_pools": {
            "image": {"dispatch": img_dispatch, "recovery": img_recovery, "dead": img_dead},
            "chat": {"dispatch": chat_d, "recovery": chat_r},
        },
        "top_buckets": buckets.most_common(20),
        "image_reasons_enabled": image_reasons.most_common(),
        "chat_reasons_enabled": chat_reasons.most_common(),
        "disabled_buckets": disabled_reasons.most_common(),
        "samples": samples,
        "notes": {
            "schedulable_177": "UI '可调度' 通常指聊轨 dispatch（fast/auto 有额度）",
            "image_dispatch_lower": "图轨 dispatch 需 imagine 额度或 L2 验证通过，通常少于聊轨",
        },
    }
    out = OUT / f"breakdown-{stamp}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"detail={out}")


if __name__ == "__main__":
    main()
