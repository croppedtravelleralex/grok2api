"""Shared helpers for Chrome ticket lifecycle batch experiments."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / ".tmp"
DEFAULT_LOG = TMP / "chrome-ticket-experiments.jsonl"
DEFAULT_SSO = TMP / "web-sso-canary-1467.json"
SSH_HOST = os.environ.get("PANDA_SSH", "panda")
PANDA_BASE = os.environ.get("PANDA_GROK2API_BASE", "http://127.0.0.1:18000").rstrip("/")
IMAGE_KEY_PATH = os.environ.get("PANDA_IMAGE_KEY_FILE", "/root/.secrets/grok2api-newapi-key")
DEFAULT_PROMPT = "a single red apple on white table, studio product photo"
IMAGINE_QUOTA_FRESH_SEC = 30 * 60
IMAGINE_UPSTREAM = "grok-imagine-image"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event: str, **fields: Any) -> None:
    row = {"ts": utc_now(), "event": event, **fields}
    print(json.dumps(row, ensure_ascii=False), flush=True)


def parse_duration(value: str) -> int:
    value = (value or "").strip().lower()
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    matched = re.fullmatch(r"(\d+(?:\.\d+)?)(s|m|h)", value)
    if not matched:
        raise ValueError(f"invalid duration: {value!r} (use 30s, 5m, 1h)")
    amount = float(matched.group(1))
    unit = matched.group(2)
    if unit == "s":
        return int(amount)
    if unit == "m":
        return int(amount * 60)
    return int(amount * 3600)


def ssh_run(command: str, *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", SSH_HOST, command],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def pin_imagine_accounts(account_ids: list[int]) -> list[int]:
    if not account_ids:
        return []
    ensure_pin_scripts()
    ids_csv = " ".join(str(x) for x in account_ids)
    proc = ssh_run(f"python3 /tmp/_panda_prepare_imagine.py {ids_csv}", timeout=max(240, 60 * len(account_ids)))
    if proc.returncode != 0:
        raise RuntimeError(f"prepare imagine failed: {(proc.stderr or proc.stdout)[:400]}")
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    return [int(x) for x in payload.get("pinned") or account_ids]


def ensure_pin_scripts() -> None:
    for name in ("_panda_pin_imagine.py", "_panda_route_check.py", "_panda_prepare_imagine.py"):
        local = ROOT / "tools" / name
        subprocess.run(
            ["scp", str(local), f"{SSH_HOST}:/tmp/{name}"],
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
        )


def admin_token() -> str:
    last_err = ""
    for attempt in range(3):
        proc = ssh_run("python3 /tmp/_panda_admin_token.py", timeout=90)
        if proc.returncode == 0 and (proc.stdout or "").strip():
            return proc.stdout.strip()
        last_err = (proc.stderr or proc.stdout)[:300]
        time.sleep(1 + attempt)
    raise RuntimeError(f"admin token failed: {last_err}")


def pool_stats() -> dict[str, Any]:
    token = admin_token()
    proc = ssh_run(
        f"curl -fsS {PANDA_BASE}/api/admin/v1/chrome-tickets/stats "
        f"-H 'Authorization: Bearer {token}'",
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pool stats failed: {(proc.stderr or proc.stdout)[:300]}")
    payload = json.loads(proc.stdout)
    return payload.get("data") or payload


def pool_available_from_stats(stats: dict[str, Any], account_id: int | None = None) -> int:
    by_status = stats.get("ByStatus") or stats.get("byStatus") or {}
    if account_id is None:
        return int(by_status.get("available") or 0)
    for row in stats.get("AvailableByAccount") or stats.get("availableByAccount") or []:
        if int(row.get("AccountID") or row.get("accountId") or row.get("account_id") or 0) == account_id:
            return int(row.get("Count") or row.get("count") or 0)
    return 0


def pool_available(account_id: int | None = None) -> int:
    return pool_available_from_stats(pool_stats(), account_id)


def imagine_generations(remaining: int, total: int) -> int | None:
    """Align with Go account.ImagineGenerations (micro-credits → 生图次数)."""
    if total <= 0 or remaining < 0:
        return None
    if total <= 1000:
        return remaining
    unit = max(1, total // 10)
    return (remaining + unit - 1) // unit


def _imagine_gens_from_account(acc: dict[str, Any]) -> int:
    for window in acc.get("quotaWindows") or []:
        if window.get("mode") != "imagine":
            continue
        total = int(window.get("total") or 0)
        remaining = int(window.get("remaining") or 0)
        gens = imagine_generations(remaining, total)
        return max(0, gens or 0)
    return 0


def imagine_remaining_by_account(account_ids: list[int] | None = None) -> dict[int, int]:
    """Read Imagine **generation count** remaining per account (not raw micro-credits).

    列表 API 常不返回 quotaWindows，指定 account_ids 时逐号 GET 详情。
    """
    token = admin_token()
    wanted = sorted({int(x) for x in (account_ids or []) if int(x) > 0})
    out: dict[int, int] = {}
    if wanted:
        for aid in wanted:
            proc = ssh_run(
                f"curl -fsS '{PANDA_BASE}/api/admin/v1/accounts/{aid}' "
                f"-H 'Authorization: Bearer {token}'",
                timeout=60,
            )
            if proc.returncode != 0:
                continue
            payload = json.loads(proc.stdout)
            acc = payload.get("data") or payload
            gens = _imagine_gens_from_account(acc)
            if gens > 0:
                out[aid] = gens
        return out
    page = 1
    while True:
        proc = ssh_run(
            f"curl -fsS '{PANDA_BASE}/api/admin/v1/accounts?provider=grok_web&page={page}&pageSize=200' "
            f"-H 'Authorization: Bearer {token}'",
            timeout=90,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"account list failed: {(proc.stderr or proc.stdout)[:300]}")
        payload = json.loads(proc.stdout)
        items = (payload.get("data") or {}).get("items") or payload.get("items") or []
        for acc in items:
            aid = int(acc.get("id") or 0)
            if not aid:
                continue
            gens = _imagine_gens_from_account(acc)
            if gens > 0:
                out[aid] = gens
        if len(items) < 200:
            break
        page += 1
    return out


def parse_account_ids_from_arg(raw: str) -> list[int] | None:
    """'dispatch' 或空 → None（由 resolve_mint_accounts 取图轨 dispatch 四池）；否则解析逗号分隔 id。"""
    value = (raw or "").strip()
    if not value or value.lower() == "dispatch":
        return None
    return [int(x.strip()) for x in value.split(",") if x.strip()]


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def iter_web_accounts() -> list[dict[str, Any]]:
    token = admin_token()
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        proc = ssh_run(
            f"curl -fsS '{PANDA_BASE}/api/admin/v1/accounts?provider=grok_web&page={page}&pageSize=200' "
            f"-H 'Authorization: Bearer {token}'",
            timeout=90,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"account list failed: {(proc.stderr or proc.stdout)[:300]}")
        payload = json.loads(proc.stdout)
        batch = (payload.get("data") or {}).get("items") or payload.get("items") or []
        items.extend(batch)
        if len(batch) < 200:
            break
        page += 1
    return items


def imagine_window_for(item: dict[str, Any]) -> dict[str, Any] | None:
    for window in item.get("quotaWindows") or []:
        if window.get("mode") == "imagine":
            return window
    return None


def imagine_model_state(item: dict[str, Any]) -> dict[str, Any] | None:
    for state in item.get("modelStates") or []:
        if state.get("upstreamModel") == IMAGINE_UPSTREAM:
            return state
    return None


def imagine_window_fresh(window: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    """与 Go imagineQuotaFresh 对齐：上游额度 30min 内同步且 remaining>0。"""
    now = now or datetime.now(timezone.utc)
    if not window or window.get("mode") != "imagine":
        return False
    if window.get("source") != "upstream":
        return False
    total = int(window.get("total") or 0)
    remaining = int(window.get("remaining") or 0)
    if total <= 0 or remaining <= 0:
        return False
    synced = _parse_iso(window.get("syncedAt"))
    if not synced:
        return False
    return (now - synced).total_seconds() <= IMAGINE_QUOTA_FRESH_SEC


def effective_imagine_status(item: dict[str, Any]) -> str | None:
    """对齐 Go modelStatesForView 对 grok-imagine-image 的合成规则。"""
    state = imagine_model_state(item)
    window = imagine_window_for(item)
    if state is None:
        return None
    status = state.get("status") or "unknown"
    if window is None:
        return status
    total = int(window.get("total") or 0)
    remaining = int(window.get("remaining") or 0)
    actual_result = status in ("available", "soft_stop", "auth_failed", "signature_failed")
    if total > 0 and remaining <= 0:
        return "quota_exhausted"
    if total > 0 and remaining > 0 and not actual_result:
        return "quota_available"
    if total == 0 and remaining == 0 and status in ("unknown", "quota_available"):
        return "unknown"
    return status


def image_dispatch_admissible(item: dict[str, Any], *, now: datetime | None = None) -> bool:
    """与 Go imageDispatchAdmissible / 四池 dispatch 准入对齐。"""
    now = now or datetime.now(timezone.utc)
    if not item.get("enabled") or item.get("authStatus") != "active":
        return False
    cooldown = _parse_iso(item.get("cooldownUntil"))
    if cooldown and cooldown > now:
        return False
    window = imagine_window_for(item)
    if not imagine_window_fresh(window, now=now):
        return False
    status = effective_imagine_status(item)
    if status is None:
        return False
    return status in ("available", "quota_available")


def image_schedulable(item: dict[str, Any], *, now: datetime | None = None) -> bool:
    """与 UI imageSchedulableAccounts 对齐：新鲜上游 Imagine 额度。"""
    return imagine_window_fresh(imagine_window_for(item), now=now)


def web_pools_snapshot() -> dict[str, Any]:
    token = admin_token()
    proc = ssh_run(
        f"curl -fsS {PANDA_BASE}/api/admin/v1/accounts/web-pools "
        f"-H 'Authorization: Bearer {token}'",
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"web-pools failed: {(proc.stderr or proc.stdout)[:300]}")
    payload = json.loads(proc.stdout)
    return payload.get("data") or payload


def web_lane_quota_summary() -> dict[str, Any]:
    token = admin_token()
    proc = ssh_run(
        f"curl -fsS {PANDA_BASE}/api/admin/v1/accounts/web-lane-quota "
        f"-H 'Authorization: Bearer {token}'",
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"web-lane-quota failed: {(proc.stderr or proc.stdout)[:300]}")
    payload = json.loads(proc.stdout)
    return payload.get("data") or payload


def ensure_panda_sso_scripts() -> None:
    for name in ("panda_export_web_sso_one.py", "panda_image_dispatch_ids.py"):
        local = ROOT / "tools" / name
        remote = f"/tmp/{name}"
        subprocess.run(
            ["scp", str(local), f"{SSH_HOST}:{remote}"],
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
        )


_DISPATCH_IDS_CACHE: tuple[float, list[int]] | None = None
_DISPATCH_IDS_CACHE_TTL = 60.0


def image_dispatch_account_ids(*, force_refresh: bool = False) -> list[int]:
    """图轨调度池账号（四池 dispatch ∩ 有 Imagine 生图次数，与 Go imageDispatchPoolIds 同源）。"""
    global _DISPATCH_IDS_CACHE
    now = time.time()
    if not force_refresh and _DISPATCH_IDS_CACHE and now - _DISPATCH_IDS_CACHE[0] < _DISPATCH_IDS_CACHE_TTL:
        return list(_DISPATCH_IDS_CACHE[1])
    ensure_panda_sso_scripts()
    pools = web_pools_snapshot()
    raw = pools.get("imageDispatchPoolIds") or []
    if raw:
        ids = sorted({int(x) for x in raw if int(x) > 0})
    else:
        proc = ssh_run("python3 /tmp/panda_image_dispatch_ids.py", timeout=180)
        if proc.returncode != 0:
            raise RuntimeError(f"dispatch ids failed: {(proc.stderr or proc.stdout)[:400]}")
        payload = json.loads((proc.stdout or "").strip().splitlines()[-1])
        ids = sorted(int(x) for x in payload.get("ids") or [])
        log_event("image_dispatch_ids_resolved", source=payload.get("source"), count=len(ids))
    _DISPATCH_IDS_CACHE = (now, ids)
    return list(ids)


def runtime_image_dispatch_account_ids(pools: dict[str, Any] | None = None) -> list[int]:
    """运行时 dispatchIndex 投影（imagePoolIds，受 pin 约束，仅供诊断对比）。"""
    pools = pools or web_pools_snapshot()
    raw = pools.get("imagePoolIds") or pools.get("image_pool_ids") or []
    return sorted({int(x) for x in raw if int(x) > 0})


def current_pin_imagine_ids(pools: dict[str, Any] | None = None) -> list[int]:
    """当前 grok-imagine-image 路由 pin 绑定（优先 API imagePinIds，否则查 Panda SQLite）。"""
    pools = pools or web_pools_snapshot()
    raw = pools.get("imagePinIds") or pools.get("image_pin_ids") or []
    if raw:
        return sorted({int(x) for x in raw if int(x) > 0})
    proc = ssh_run(
        "sqlite3 /opt/grok2api/data/backend.db "
        "'SELECT account_id FROM model_route_accounts WHERE model_route_id=5 ORDER BY account_id'",
        timeout=30,
    )
    if proc.returncode != 0:
        return []
    out: list[int] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            out.append(int(line))
    return out


def image_dispatch_target_ids(*, force_refresh: bool = False) -> list[int]:
    """四池 dispatch 目标集（imageDispatchAdmissible，与 Go summarizeWebPools 同源）。"""
    global _DISPATCH_IDS_CACHE
    pools = web_pools_snapshot()
    raw = pools.get("imageDispatchPoolIds") or []
    if raw:
        ids = sorted({int(x) for x in raw if int(x) > 0})
    else:
        now = datetime.now(timezone.utc)
        ids = sorted(
            int(item["id"])
            for item in iter_web_accounts()
            if int(item.get("id") or 0) > 0 and image_dispatch_admissible(item, now=now)
        )
    imagine = imagine_remaining_by_account(ids) if ids else {}
    with_quota = sorted(aid for aid in ids if imagine.get(aid, 0) > 0)
    if force_refresh:
        _DISPATCH_IDS_CACHE = (time.time(), with_quota)
    return with_quota


def pin_imagine_replace_only(account_ids: list[int]) -> list[int]:
    """仅替换 grok-imagine-image pin + reconcile（不逐号 refresh quota）。"""
    ensure_pin_scripts()
    if not account_ids:
        proc = ssh_run(
            "sqlite3 /opt/grok2api/data/backend.db "
            "'DELETE FROM model_route_accounts WHERE model_route_id=5'",
            timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pin clear failed: {(proc.stderr or proc.stdout)[:300]}")
        pinned: list[int] = []
    else:
        ids_csv = " ".join(str(x) for x in account_ids)
        proc = ssh_run(f"python3 /tmp/_panda_pin_imagine.py {ids_csv}", timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"pin replace failed: {(proc.stderr or proc.stdout)[:300]}")
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        pinned = [int(x) for x in payload.get("pinned") or account_ids]
    token = admin_token()
    proc = ssh_run(
        f"curl -fsS -X POST {PANDA_BASE}/api/admin/v1/accounts/web-pools/reconcile "
        f"-H 'Authorization: Bearer {token}' -H 'Content-Type: application/json' -d '{{}}'",
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"reconcile after pin failed: {(proc.stderr or proc.stdout)[:300]}")
    global _DISPATCH_IDS_CACHE
    _DISPATCH_IDS_CACHE = None
    return pinned


def sync_image_dispatch_pins(*, dry_run: bool = False, use_api: bool = True) -> dict[str, Any]:
    """将 pin/runtime 与四池 dispatch（有 Imagine 额度）自动对齐。"""
    if use_api:
        token = admin_token()
        proc = ssh_run(
            f"curl -fsS -X POST {PANDA_BASE}/api/admin/v1/accounts/web-pools/sync-dispatch-pins "
            f"-H 'Authorization: Bearer {token}' -H 'Content-Type: application/json' -d '{{}}'",
            timeout=180,
        )
        if proc.returncode == 0:
            payload = json.loads(proc.stdout)
            data = payload.get("data") or payload
            global _DISPATCH_IDS_CACHE
            _DISPATCH_IDS_CACHE = None
            report = {
                "ok": True,
                "mode": "api",
                "changed": bool(data.get("changed")),
                "target": data.get("targetIds") or data.get("target_ids") or [],
                "previous": data.get("previousIds") or data.get("previous_ids") or [],
                "added": data.get("addedIds") or data.get("added_ids") or [],
                "removed": data.get("removedIds") or data.get("removed_ids") or [],
                "snapshot": data.get("snapshot") or {},
            }
            log_event("dispatch_pin_sync", **{k: v for k, v in report.items() if k != "snapshot"})
            return report
        if proc.returncode != 0 and "404" not in (proc.stderr or proc.stdout):
            log_event("dispatch_pin_sync_api_fallback", http_err=(proc.stderr or proc.stdout)[:200])

    target = set(image_dispatch_target_ids(force_refresh=True))
    previous = set(current_pin_imagine_ids())
    added = sorted(target - previous)
    removed = sorted(previous - target)
    changed = target != previous
    report: dict[str, Any] = {
        "ok": True,
        "mode": "ssh_pin",
        "changed": changed,
        "target": sorted(target),
        "previous": sorted(previous),
        "added": added,
        "removed": removed,
    }
    if dry_run:
        report["dry_run"] = True
        log_event("dispatch_pin_sync_dry_run", **{k: v for k, v in report.items() if k != "dry_run"})
        return report
    if changed:
        report["pinned"] = pin_imagine_replace_only(sorted(target))
        report["snapshot"] = web_pools_snapshot()
    else:
        report["pinned"] = sorted(previous)
    log_event("dispatch_pin_sync", **{k: v for k, v in report.items() if k not in ("snapshot", "pinned")})
    return report


def resolve_mint_accounts(
    explicit: list[int] | None,
    *,
    sso_file: Path,
    require_sso: bool = True,
) -> list[int]:
    """图轨调度池（dispatch）∩（可选 SSO 文件中有凭证的号）。非 dispatch 号一律排除。"""
    dispatch = set(image_dispatch_account_ids())
    if explicit:
        rejected = sorted({int(x) for x in explicit if int(x) > 0} - dispatch)
        if rejected:
            log_event("mint_accounts_rejected_not_dispatch", account_ids=rejected[:20], rejected_count=len(rejected))
        dispatch &= {int(x) for x in explicit if int(x) > 0}
    accounts = load_accounts(sso_file) if sso_file.exists() else {}
    if require_sso and accounts:
        dispatch = {aid for aid in dispatch if aid in accounts}
    selected = sorted(dispatch)
    pools = web_pools_snapshot()
    log_event(
        "mint_accounts_resolved",
        image_dispatch_pool=len(image_dispatch_account_ids()),
        runtime_dispatch_ids=runtime_image_dispatch_account_ids(pools),
        four_pools_image=(pools.get("fourPools") or {}).get("image"),
        selected=selected,
        selected_count=len(selected),
        sso_file=str(sso_file),
    )
    return selected


def imagine_quota_cap(account_id: int) -> int:
    """账号当前 Imagine 可生图次数上限（换算后）。"""
    return imagine_remaining_by_account([account_id]).get(account_id, 0)


def effective_mint_target(account_id: int, base_target: int) -> int:
    """票池深度目标 = min(池深目标, Imagine 剩余次数)。"""
    cap = imagine_quota_cap(account_id)
    if cap <= 0:
        return 0
    return min(base_target, cap)


def mint_headroom(account_id: int, base_target: int, *, pool_stats_snapshot: dict[str, Any] | None = None) -> int:
    """在额度上限内，距离目标池深还差多少张。"""
    cap = imagine_quota_cap(account_id)
    if cap <= 0:
        return 0
    stats = pool_stats_snapshot or pool_stats()
    available = pool_available_from_stats(stats, account_id)
    if available >= cap:
        return 0
    target = min(base_target, cap)
    return max(0, target - available)


def ticket_quota_audit(account_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """检查每账号：Imagine 剩余 >= 池中 available 票数。"""
    imagine = imagine_remaining_by_account(account_ids)
    stats = pool_stats()
    rows: list[dict[str, Any]] = []
    for item in stats.get("AvailableByAccount") or stats.get("availableByAccount") or []:
        aid = int(item.get("AccountID") or item.get("accountId") or item.get("account_id") or 0)
        if not aid or (account_ids and aid not in account_ids):
            continue
        avail = int(item.get("Count") or item.get("count") or 0)
        quota = imagine.get(aid, 0)
        rows.append(
            {
                "account_id": aid,
                "available_tickets": avail,
                "imagine_remaining": quota,
                "over_by": max(0, avail - quota),
                "ok": avail <= quota,
            }
        )
    dispatch = set(image_dispatch_account_ids())
    for aid in sorted(dispatch):
        if account_ids and aid not in account_ids:
            continue
        if any(r["account_id"] == aid for r in rows):
            continue
        quota = imagine.get(aid, 0)
        rows.append(
            {
                "account_id": aid,
                "available_tickets": 0,
                "imagine_remaining": quota,
                "over_by": 0,
                "ok": True,
            }
        )
    return sorted(rows, key=lambda r: (-r["over_by"], r["account_id"]))


def push_ticket(row: dict[str, Any], *, ttl_hours: float = 12.0) -> dict[str, Any]:
    payload = {
        "account_id": int(row["account_id"]),
        "statsig_meta": row.get("statsig_meta") or "",
        "cookie": row.get("cookie") or "",
        "user_agent": row.get("user_agent") or "",
        "sign_source": row.get("sign_source") or "",
        "ttl_hours": ttl_hours,
    }
    token = admin_token()
    body = json.dumps(payload, ensure_ascii=False)
    proc = ssh_run(
        f"curl -fsS -X POST {PANDA_BASE}/api/admin/v1/chrome-tickets "
        f"-H 'Authorization: Bearer {token}' -H 'Content-Type: application/json' "
        f"-d {json.dumps(body)}",
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"push ticket failed: {(proc.stderr or proc.stdout)[:400]}")
    data = json.loads(proc.stdout).get("data") or json.loads(proc.stdout)
    return data


def load_accounts(path: Path) -> dict[int, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("accounts") if isinstance(data, dict) else data
    return {int(a["id"]): a for a in rows if a.get("sso")}


def load_chrome_modules():
    spec_c = importlib.util.spec_from_file_location("chrome", ROOT / "tools" / "_chrome_channel_chat_image.py")
    chrome = importlib.util.module_from_spec(spec_c)
    assert spec_c and spec_c.loader
    spec_c.loader.exec_module(chrome)
    spec_v = importlib.util.spec_from_file_location("v1", ROOT / "tools" / "web_http_chat_image_canary.v1.py")
    v1 = importlib.util.module_from_spec(spec_v)
    assert spec_v and spec_v.loader
    spec_v.loader.exec_module(v1)
    return chrome, v1


def sanitize_cookie(cookie: str) -> str:
    keep: list[str] = []
    for part in (cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name = part.split("=", 1)[0].strip().lower()
        if name in ("sso", "sso-rw", "cf_clearance", "__cf_bm"):
            continue
        keep.append(part)
    return "; ".join(keep)


def fetch_sso_ephemeral(account_id: int) -> str:
    """从 Panda DB 解密单号 SSO（仅内存，不落盘）。"""
    ensure_panda_sso_scripts()
    proc = ssh_run(f"python3 /tmp/panda_export_web_sso_one.py {int(account_id)}", timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"sso export failed: {(proc.stderr or proc.stdout)[:300]}")
    line = (proc.stdout or "").strip().splitlines()[-1]
    payload = json.loads(line)
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    token = (payload.get("sso") or "").strip()
    if len(token) < 20:
        raise RuntimeError("sso_empty_or_too_short")
    log_event(
        "jit_sso_fetched",
        account_id=account_id,
        token_len=payload.get("token_len"),
        token_prefix=payload.get("token_prefix"),
    )
    return token


def mint_ticket_row_from_sso(account_id: int, sso_token: str, *, timeout: int = 120) -> dict[str, Any]:
    chrome, v1 = load_chrome_modules()
    signed = chrome.sign_with_chrome(sso_token, timeout_s=timeout, proxy=v1.PROXY)
    meta = signed.get("statsigMeta") or ""
    if not meta:
        raise RuntimeError(f"mint failed: {signed.get('error') or 'no statsig_meta'}")
    return {
        "account_id": account_id,
        "statsig_meta": meta,
        "cookie": sanitize_cookie(signed.get("cookie") or ""),
        "user_agent": getattr(chrome, "UA", "") or signed.get("userAgent") or "",
        "sign_source": signed.get("source") or "",
    }


def mint_ticket_row(account_id: int, sso_file: Path, *, timeout: int = 120) -> dict[str, Any]:
    accounts = load_accounts(sso_file)
    acc = accounts.get(account_id)
    if not acc:
        raise RuntimeError(f"account {account_id} missing in {sso_file}")
    return mint_ticket_row_from_sso(account_id, acc["sso"], timeout=timeout)


def dispatch_mint_worklist(base_target: int, *, dispatch_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """实时调度池 + 额度/池深，返回待灌票工作项。"""
    dispatch = dispatch_ids or image_dispatch_account_ids()
    target = base_target if base_target > 0 else max(1, (10 + len(dispatch) - 1) // max(len(dispatch), 1))
    stats = pool_stats()
    imagine = imagine_remaining_by_account(dispatch)
    work: list[dict[str, Any]] = []
    for aid in dispatch:
        cap = imagine.get(aid, 0)
        if cap <= 0:
            continue
        available = pool_available_from_stats(stats, aid)
        eff_target = min(target, cap)
        if available >= cap or available >= eff_target:
            continue
        headroom = max(0, eff_target - available)
        if headroom <= 0:
            continue
        work.append(
            {
                "account_id": aid,
                "headroom": headroom,
                "imagine_cap": cap,
                "pool_available": available,
                "target": eff_target,
            }
        )
    work.sort(key=lambda row: (-row["headroom"], row["account_id"]))
    return work


def jit_mint_one(account_id: int, *, timeout: int = 120, ttl_hours: float = 12.0) -> dict[str, Any]:
    """JIT：校验 dispatch + 额度 → 拉 SSO → 开票 → 回传 → 丢弃 SSO（不落盘）。"""
    aid = int(account_id)
    cap = imagine_quota_cap(aid)
    if cap <= 0:
        receipt = {"ok": False, "account_id": aid, "stage": "quota_check", "error": "no_imagine_quota"}
        log_event("jit_mint_skip", **receipt)
        return receipt
    if aid not in set(image_dispatch_account_ids()):
        receipt = {"ok": False, "account_id": aid, "stage": "dispatch_check", "error": "not_in_image_dispatch_pool"}
        log_event("jit_mint_skip", **receipt)
        return receipt
    sso_token: str | None = None
    try:
        sso_token = fetch_sso_ephemeral(aid)
        before_avail = pool_available(aid)
        row = mint_ticket_row_from_sso(aid, sso_token, timeout=timeout)
        pushed = push_ticket(row, ttl_hours=ttl_hours)
        after_avail = pool_available(aid)
        receipt = {
            "ok": True,
            "account_id": aid,
            "ticket_id": pushed.get("id"),
            "expires_at": pushed.get("expires_at"),
            "pool_before": before_avail,
            "pool_after": after_avail,
            "pool_delta": after_avail - before_avail,
            "meta_len": len(row.get("statsig_meta") or ""),
        }
        if receipt["pool_delta"] < 1:
            receipt["warn"] = "pool_not_increased"
        log_event("jit_mint_ok", **{k: v for k, v in receipt.items() if k != "ok"})
        return receipt
    except Exception as exc:
        receipt = {"ok": False, "account_id": aid, "error": str(exc)[:300]}
        log_event("jit_mint_failed", **receipt)
        return receipt
    finally:
        sso_token = None


def run_minter(account_ids: list[int], sso_file: Path, *, timeout: int = 120) -> bool:
    cmd = [
        sys.executable,
        str(ROOT / "tools" / "chrome_ticket_pool_minter.py"),
        "--account-ids",
        ",".join(str(x) for x in account_ids),
        "--sso-file",
        str(sso_file),
        "--timeout",
        str(timeout),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode == 0


def mint_to_pool(account_id: int, sso_file: Path, *, timeout: int = 120, ttl_hours: float = 12.0) -> dict[str, Any]:
    row = mint_ticket_row(account_id, sso_file, timeout=timeout)
    pushed = push_ticket(row, ttl_hours=ttl_hours)
    log_event("mint_to_pool", account_id=account_id, ticket_id=pushed.get("id"), expires_at=pushed.get("expires_at"))
    return {"row": row, "pushed": pushed, "minted_at": utc_now()}


def ensure_pool_depth(account_id: int, sso_file: Path, needed: int, *, timeout: int = 120) -> int:
    capped = effective_mint_target(account_id, needed)
    if capped <= 0:
        log_event("ensure_pool_depth_skip", account_id=account_id, reason="no_imagine_quota", needed=needed)
        return 0
    available = pool_available(account_id)
    minted = 0
    while available < capped:
        mint_to_pool(account_id, sso_file, timeout=timeout)
        minted += 1
        available = pool_available(account_id)
        if available >= capped or minted > capped + 2:
            break
    return minted


def run_remote_python(script: str, *, remote_name: str, timeout: int = 240) -> str:
    TMP.mkdir(parents=True, exist_ok=True)
    local = TMP / remote_name
    local.write_text(script.strip() + "\n", encoding="utf-8", newline="\n")
    remote = f"/tmp/{remote_name}"
    scp = subprocess.run(
        ["scp", str(local), f"{SSH_HOST}:{remote}"],
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    if scp.returncode != 0:
        raise RuntimeError(f"scp script failed: {(scp.stderr or scp.stdout)[:300]}")
    proc = ssh_run(f"sed -i 's/\\r$//' {remote} && python3 {remote}", timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"remote python failed: {(proc.stderr or proc.stdout)[:400]}")
    return proc.stdout.strip()


def panda_image_once(
    *,
    prompt: str = DEFAULT_PROMPT,
    log_since_s: int = 180,
) -> dict[str, Any]:
    script = f"""
import json, subprocess, time, urllib.error, urllib.request
KEY=open({IMAGE_KEY_PATH!r}).read().strip()
BASE={PANDA_BASE!r}
PROMPT={prompt!r}
started=time.time()
body=json.dumps({{"model":"grok-imagine-image","prompt":PROMPT,"size":"1024x1024","n":1}}).encode()
req=urllib.request.Request(BASE+"/v1/images/generations", data=body, method="POST", headers={{
    "Authorization":"Bearer "+KEY, "Content-Type":"application/json"}})
http=0; err=""; url=""
try:
    with urllib.request.urlopen(req, timeout=180) as resp:
        http=resp.status; raw=resp.read()
        data=json.loads(raw.decode("utf-8","replace")).get("data") or []
        if data:
            url=(data[0].get("url") or "")[:120]
except urllib.error.HTTPError as exc:
    http=exc.code; err=exc.read().decode("utf-8","replace")[:300]
wall_ms=int((time.time()-started)*1000)
logs=subprocess.run(["docker","logs","grok2api","--since","{log_since_s}s"], capture_output=True, text=True)
text=logs.stdout or ""
pool_hit="chrome_ticket_pool_hit" in text
asset403=sum(1 for line in text.splitlines() if "web_lite_asset_download_failed" in line and "403" in line)
cf_warm=any("web_lite_asset_cf_warm" in line for line in text.splitlines())
print(json.dumps({{"http":http,"wall_ms":wall_ms,"pool_hit":pool_hit,"asset403":asset403,"cf_warm":cf_warm,"url":url,"error":err}}))
"""
    out = run_remote_python(script, remote_name="_chrome_ticket_image_once.py", timeout=240)
    line = out.splitlines()[-1]
    return json.loads(line)


def append_experiment_log(batch: str, *, log_path: Path = DEFAULT_LOG, **fields: Any) -> dict[str, Any]:
    TMP.mkdir(parents=True, exist_ok=True)
    row = {"batch": batch, "ts": utc_now(), **fields}
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    log_event("experiment_logged", batch=batch, log=str(log_path))
    return row


def verdict(http: int, pool_hit: bool, *, require_pool_hit: bool = True) -> str:
    if http == 200 and (pool_hit or not require_pool_hit):
        return "pass"
    if http in (403, 502, 429):
        return "fail"
    return "inconclusive"


def sleep_countdown(seconds: int, *, label: str = "wait") -> None:
    if seconds <= 0:
        return
    log_event(f"{label}_start", seconds=seconds)
    end = time.time() + seconds
    while True:
        remaining = int(end - time.time())
        if remaining <= 0:
            break
        if remaining % 60 == 0 or remaining <= 10:
            log_event(f"{label}_tick", remaining_s=remaining)
        time.sleep(min(5, remaining))
    log_event(f"{label}_done", seconds=seconds)
