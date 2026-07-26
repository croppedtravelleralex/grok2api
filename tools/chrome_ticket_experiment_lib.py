"""Shared helpers for Chrome ticket lifecycle batch experiments."""

from __future__ import annotations

import importlib.util
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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
DEFAULT_QUOTA_FETCH_WORKERS = int(os.environ.get("CHROME_TICKET_QUOTA_WORKERS", "12"))


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
    override = (os.environ.get("PANDA_ADMIN_TOKEN") or "").strip()
    if override:
        return override
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


def _fetch_account_imagine_gens(aid: int, token: str) -> tuple[int, int]:
    proc = ssh_run(
        f"curl -fsS '{PANDA_BASE}/api/admin/v1/accounts/{aid}' "
        f"-H 'Authorization: Bearer {token}'",
        timeout=60,
    )
    if proc.returncode != 0:
        return aid, 0
    payload = json.loads(proc.stdout)
    acc = payload.get("data") or payload
    return aid, _imagine_gens_from_account(acc)


def _imagine_remaining_remote_batch(account_ids: list[int], token: str, *, workers: int) -> dict[int, int]:
    """在 Panda 本机并发拉取账号 Imagine 次数（单次 SSH，避免 109 次往返）。"""
    ids = sorted({int(x) for x in account_ids if int(x) > 0})
    if not ids:
        return {}
    workers = max(1, min(workers, 32))
    script = r'''
import json, os, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ["PANDA_BASE"]
TOKEN = os.environ["ADMIN_TOKEN"]
IDS = json.loads(os.environ["ACCOUNT_IDS"])
WORKERS = int(os.environ.get("WORKERS", "12"))

def imagine_generations(remaining, total):
    if total <= 0 or remaining < 0:
        return None
    if total <= 1000:
        return remaining
    unit = max(1, total // 10)
    return (remaining + unit - 1) // unit

def gens_from_acc(acc):
    for window in acc.get("quotaWindows") or []:
        if window.get("mode") != "imagine":
            continue
        total = int(window.get("total") or 0)
        remaining = int(window.get("remaining") or 0)
        g = imagine_generations(remaining, total)
        return max(0, g or 0)
    return 0

def fetch_one(aid):
    req = urllib.request.Request(
        f"{BASE}/api/admin/v1/accounts/{aid}",
        headers={"Authorization": "Bearer " + TOKEN},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        acc = payload.get("data") or payload
        return aid, gens_from_acc(acc)
    except Exception:
        return aid, 0

out = {}
with ThreadPoolExecutor(max_workers=WORKERS) as pool:
    for aid, gens in pool.map(fetch_one, IDS):
        if gens > 0:
            out[str(aid)] = gens
print(json.dumps(out, ensure_ascii=False))
'''
    env_prefix = (
        f"PANDA_BASE={PANDA_BASE!r} ADMIN_TOKEN={token!r} "
        f"ACCOUNT_IDS={json.dumps(ids)!r} WORKERS={workers}"
    )
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    print(
        f"[quota] remote batch n={len(ids)} workers={workers}",
        file=sys.stderr,
        flush=True,
    )
    proc = ssh_run(
        f"{env_prefix} python3 -c 'import base64; exec(base64.b64decode(\"{b64}\"))'",
        timeout=max(120, 30 + len(ids) * 2),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"remote quota batch failed: {(proc.stderr or proc.stdout)[:400]}")
    line = (proc.stdout or "").strip().splitlines()[-1]
    raw = json.loads(line)
    return {int(k): int(v) for k, v in raw.items()}


def imagine_remaining_by_account(
    account_ids: list[int] | None = None,
    *,
    workers: int | None = None,
) -> dict[int, int]:
    """Read Imagine **generation count** remaining per account (not raw micro-credits).

    列表 API 常不返回 quotaWindows，指定 account_ids 时逐号 GET 详情（大批量走 Panda 本机并发批处理）。
    """
    token = admin_token()
    wanted = sorted({int(x) for x in (account_ids or []) if int(x) > 0})
    pool_workers = workers if workers is not None else DEFAULT_QUOTA_FETCH_WORKERS
    out: dict[int, int] = {}
    if wanted:
        if len(wanted) >= 8:
            return _imagine_remaining_remote_batch(wanted, token, workers=pool_workers)
        with ThreadPoolExecutor(max_workers=min(pool_workers, max(1, len(wanted)))) as executor:
            futures = [executor.submit(_fetch_account_imagine_gens, aid, token) for aid in wanted]
            for fut in as_completed(futures):
                aid, gens = fut.result()
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


def ensure_unified_snapshot_script() -> None:
    local = ROOT / "tools" / "panda_unified_pool_snapshot.py"
    remote = f"{SSH_HOST}:/tmp/panda_unified_pool_snapshot.py"
    subprocess.run(
        ["scp", str(local), remote],
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )


def unified_pool_snapshot(*, workers: int | None = None) -> dict[str, Any]:
    """单次 SSH：web-pools + ticket stats + lane-quota + dispatch 额度批处理。"""
    ensure_unified_snapshot_script()
    pool_workers = workers if workers is not None else DEFAULT_QUOTA_FETCH_WORKERS
    proc = ssh_run(
        f"python3 /tmp/panda_unified_pool_snapshot.py --workers {pool_workers}",
        timeout=max(120, 60 + pool_workers * 5),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"unified snapshot failed: {(proc.stderr or proc.stdout)[:400]}")
    line = (proc.stdout or "").strip().splitlines()[-1]
    return json.loads(line)


def _imagine_map_from_snapshot(snapshot: dict[str, Any]) -> dict[int, int]:
    raw = snapshot.get("imagine_by_account") or {}
    return {int(k): int(v) for k, v in raw.items()}


def ticket_quota_audit_from_snapshot(
    snapshot: dict[str, Any],
    account_ids: list[int] | None = None,
    *,
    include_dispatch_without_tickets: bool = False,
) -> list[dict[str, Any]]:
    """基于 unified snapshot 的票↔额度对账（无额外 SSH）。"""
    pools = snapshot.get("web_pools") or {}
    stats = snapshot.get("pool_stats") or {}
    imagine = _imagine_map_from_snapshot(snapshot)
    dispatch = set(image_dispatch_account_ids_from_pools(pools))
    pin = set(current_pin_imagine_ids(pools))
    runtime = set(runtime_image_dispatch_account_ids(pools))
    holder_ids: list[int] = []
    for item in stats.get("AvailableByAccount") or stats.get("availableByAccount") or []:
        aid = int(item.get("AccountID") or item.get("accountId") or item.get("account_id") or 0)
        if not aid or (account_ids and aid not in account_ids):
            continue
        holder_ids.append(aid)
    scan_ids = sorted(account_ids) if account_ids else sorted(dispatch)
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
                "in_dispatch": aid in dispatch,
                "in_pin": aid in pin,
                "in_runtime": aid in runtime,
                "orphan_ticket": avail > 0 and aid not in dispatch,
            }
        )
    if include_dispatch_without_tickets:
        for aid in scan_ids:
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
                    "in_dispatch": aid in dispatch,
                    "in_pin": aid in pin,
                    "in_runtime": aid in runtime,
                    "orphan_ticket": False,
                }
            )
    return sorted(rows, key=lambda r: (-r["over_by"], r["account_id"]))


def expire_available_tickets(account_ids: list[int]) -> dict[str, Any]:
    """将指定账号的 available 票标记为 expired（运维清扫，无 Admin DELETE API）。"""
    ids = sorted({int(x) for x in account_ids if int(x) > 0})
    if not ids:
        return {"expired_rows": 0, "account_ids": []}
    in_clause = ",".join(str(x) for x in ids)
    sql = (
        "UPDATE chrome_tickets SET status='expired' "
        f"WHERE status='available' AND account_id IN ({in_clause}); "
        "SELECT changes();"
    )
    proc = ssh_run(f"sqlite3 /opt/grok2api/data/backend.db {json.dumps(sql)}", timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"expire tickets failed: {(proc.stderr or proc.stdout)[:300]}")
    line = (proc.stdout or "").strip().splitlines()[-1]
    expired_rows = int(line) if line.isdigit() else 0
    out = {"expired_rows": expired_rows, "account_ids": ids}
    log_event("expire_available_tickets", **out)
    return out


def orphan_ticket_accounts_from_snapshot(snapshot: dict[str, Any]) -> list[int]:
    pools = snapshot.get("web_pools") or {}
    dispatch = image_dispatch_account_ids_from_pools(pools)
    audit = ticket_quota_audit_from_snapshot(
        snapshot,
        dispatch,
        include_dispatch_without_tickets=True,
    )
    return sorted({int(r["account_id"]) for r in audit if r.get("orphan_ticket")})


def purge_orphan_tickets(
    *,
    snapshot: dict[str, Any] | None = None,
    workers: int | None = None,
    account_ids: list[int] | None = None,
) -> dict[str, Any]:
    """清除非 dispatch 账号上的 available 孤儿票。"""
    snap = snapshot or unified_pool_snapshot(workers=workers)
    orphans = sorted({int(x) for x in (account_ids or []) if int(x) > 0}) or orphan_ticket_accounts_from_snapshot(snap)
    expired = expire_available_tickets(orphans) if orphans else {"expired_rows": 0, "account_ids": []}
    report = {"orphans": orphans, "expired": expired}
    log_event("purge_orphan_tickets", orphans=orphans, expired_rows=expired.get("expired_rows"))
    return report


def remediate_pool_probe(
    *,
    target_concurrent: int = 5,
    target_tickets: int = 20,
    workers: int | None = None,
    sync_pins: bool = True,
) -> dict[str, Any]:
    """清孤儿票 + pin sync，返回最新探针报告。"""
    snapshot = unified_pool_snapshot(workers=workers)
    orphans = orphan_ticket_accounts_from_snapshot(snapshot)
    purge_report = purge_orphan_tickets(snapshot=snapshot, account_ids=orphans) if orphans else None
    pin_report = sync_image_dispatch_pins() if sync_pins else None
    report = ticket_pool_probe(
        target_concurrent=target_concurrent,
        target_tickets=target_tickets,
        workers=workers,
        use_unified_snapshot=True,
    )
    report["remediation"] = {
        "orphans_before": orphans,
        "purge": purge_report,
        "pin_sync": pin_report,
    }
    log_event(
        "pool_probe_remediate",
        ready_both=report["readiness"]["ready_for_both"],
        blockers=report["readiness"]["blockers"],
        orphans_purged=purge_report,
    )
    return report


def image_dispatch_account_ids_from_pools(pools: dict[str, Any]) -> list[int]:
    raw = pools.get("imageDispatchPoolIds") or []
    return sorted({int(x) for x in raw if int(x) > 0})


def preflight_mint_gate(
    *,
    workers: int | None = None,
    sync_pins: bool = True,
    dispatch_ids: list[int] | None = None,
) -> dict[str, Any]:
    """JIT 开票前门控：统一快照 → 对账 → 必要时 pin sync → 返回可 mint 账号集合。"""
    snapshot = unified_pool_snapshot(workers=workers)
    pools = snapshot.get("web_pools") or {}
    dispatch = dispatch_ids or image_dispatch_account_ids_from_pools(pools)
    audit = ticket_quota_audit_from_snapshot(
        snapshot,
        dispatch,
        include_dispatch_without_tickets=True,
    )
    violations = [r for r in audit if not r["ok"]]
    orphans = [int(r["account_id"]) for r in audit if r.get("orphan_ticket")]
    pin_not_in_dispatch = pools.get("pinNotInDispatch") or []
    pin_sync_report: dict[str, Any] | None = None

    if sync_pins and (pin_not_in_dispatch or orphans):
        if orphans:
            purge_orphan_tickets(snapshot=snapshot, account_ids=orphans)
        pin_sync_report = sync_image_dispatch_pins()
        snapshot = unified_pool_snapshot(workers=workers)
        pools = snapshot.get("web_pools") or {}
        dispatch = dispatch_ids or image_dispatch_account_ids_from_pools(pools)
        audit = ticket_quota_audit_from_snapshot(
            snapshot,
            dispatch,
            include_dispatch_without_tickets=True,
        )
        violations = [r for r in audit if not r["ok"]]
        orphans = [int(r["account_id"]) for r in audit if r.get("orphan_ticket")]
        pin_not_in_dispatch = pools.get("pinNotInDispatch") or []

    blocked_ids = {int(r["account_id"]) for r in violations} | set(orphans)
    mintable_dispatch = [aid for aid in dispatch if aid not in blocked_ids]
    gate_ok = not violations and not orphans and not pin_not_in_dispatch
    report = {
        "ok": gate_ok,
        "violations": violations,
        "orphans": orphans,
        "pin_not_in_dispatch": pin_not_in_dispatch,
        "blocked_account_ids": sorted(blocked_ids),
        "mintable_dispatch": mintable_dispatch,
        "pin_sync": pin_sync_report,
        "snapshot_meta": snapshot.get("meta"),
    }
    log_event(
        "preflight_mint_gate",
        ok=gate_ok,
        violations=len(violations),
        orphans=orphans,
        pin_not_in_dispatch=pin_not_in_dispatch,
        mintable=len(mintable_dispatch),
    )
    return {"report": report, "snapshot": snapshot, "audit": audit}


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


def ticket_quota_audit(
    account_ids: list[int] | None = None,
    *,
    include_dispatch_without_tickets: bool = False,
    workers: int | None = None,
    snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """检查每账号：Imagine 剩余 >= 池中 available 票数；并标注 dispatch/pin/runtime 对齐。"""
    if snapshot is not None:
        return ticket_quota_audit_from_snapshot(
            snapshot,
            account_ids,
            include_dispatch_without_tickets=include_dispatch_without_tickets,
        )
    stats = pool_stats()
    pools = web_pools_snapshot()
    dispatch = set(image_dispatch_account_ids())
    pin = set(current_pin_imagine_ids(pools))
    runtime = set(runtime_image_dispatch_account_ids(pools))
    holder_ids: list[int] = []
    for item in stats.get("AvailableByAccount") or stats.get("availableByAccount") or []:
        aid = int(item.get("AccountID") or item.get("accountId") or item.get("account_id") or 0)
        if not aid or (account_ids and aid not in account_ids):
            continue
        holder_ids.append(aid)
    scan_ids = sorted(account_ids) if account_ids else sorted(dispatch)
    if include_dispatch_without_tickets:
        quota_ids = sorted(set(holder_ids) | set(scan_ids))
    elif account_ids:
        quota_ids = sorted(set(holder_ids) | set(account_ids))
    else:
        quota_ids = sorted(holder_ids)
    imagine = imagine_remaining_by_account(quota_ids or None, workers=workers)
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
                "in_dispatch": aid in dispatch,
                "in_pin": aid in pin,
                "in_runtime": aid in runtime,
                "orphan_ticket": avail > 0 and aid not in dispatch,
            }
        )
    if include_dispatch_without_tickets:
        for aid in scan_ids:
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
                    "in_dispatch": aid in dispatch,
                    "in_pin": aid in pin,
                    "in_runtime": aid in runtime,
                    "orphan_ticket": False,
                }
            )
    return sorted(rows, key=lambda r: (-r["over_by"], r["account_id"]))


def ticket_pool_probe(
    *,
    target_concurrent: int = 5,
    target_tickets: int = 20,
    workers: int | None = None,
    use_unified_snapshot: bool = True,
) -> dict[str, Any]:
    """票池探针：票↔额度对账、pin/runtime 对齐、开票工作项与 N 并发×M 票就绪度。"""
    pool_workers = workers if workers is not None else DEFAULT_QUOTA_FETCH_WORKERS
    snapshot: dict[str, Any] | None = None
    if use_unified_snapshot:
        print(
            f"[probe] unified snapshot workers={pool_workers}…",
            file=sys.stderr,
            flush=True,
        )
        t0 = time.monotonic()
        snapshot = unified_pool_snapshot(workers=pool_workers)
        print(f"[probe] snapshot done in {time.monotonic() - t0:.1f}s", file=sys.stderr, flush=True)
        pools = snapshot.get("web_pools") or {}
        quota = snapshot.get("lane_quota") or {}
        stats = snapshot.get("pool_stats") or {}
    else:
        pools = web_pools_snapshot()
        quota = web_lane_quota_summary()
        stats = pool_stats()
    by_status = stats.get("ByStatus") or stats.get("byStatus") or {}
    available_total = int(by_status.get("available") or 0)
    if snapshot is not None:
        dispatch = image_dispatch_account_ids_from_pools(pools)
    else:
        dispatch = image_dispatch_account_ids(force_refresh=True)
    pin = current_pin_imagine_ids(pools)
    runtime = runtime_image_dispatch_account_ids(pools)
    print(
        f"[probe] dispatch={len(dispatch)} auditing…",
        file=sys.stderr,
        flush=True,
    )
    t0 = time.monotonic()
    audit_rows = ticket_quota_audit(
        dispatch,
        include_dispatch_without_tickets=True,
        workers=pool_workers,
        snapshot=snapshot,
    )
    print(f"[probe] audit done in {time.monotonic() - t0:.1f}s", file=sys.stderr, flush=True)
    violations = [r for r in audit_rows if not r["ok"]]
    ticket_holders = [r for r in audit_rows if int(r.get("available_tickets") or 0) > 0]
    schedulable_runtime = [r for r in ticket_holders if r.get("in_runtime")]
    imagine_cache = {int(r["account_id"]): int(r["imagine_remaining"]) for r in audit_rows}
    work = dispatch_mint_worklist(
        max(1, (target_tickets + max(len(dispatch), 1) - 1) // max(len(dispatch), 1)),
        dispatch_ids=dispatch,
        workers=pool_workers,
        imagine_cache=imagine_cache,
    )
    mint_headroom_total = sum(int(w.get("headroom") or 0) for w in work)
    blockers: list[str] = []
    if available_total < target_tickets:
        blockers.append(f"ticket_pool_available={available_total} < target_tickets={target_tickets}")
    if len(schedulable_runtime) < target_concurrent:
        blockers.append(
            f"runtime_ticket_accounts={len(schedulable_runtime)} < target_concurrent={target_concurrent}"
        )
    orphans = [r["account_id"] for r in ticket_holders if r.get("orphan_ticket")]
    if orphans:
        blockers.append(f"orphan_tickets_on_non_dispatch={orphans}")
    pin_not_in_dispatch = pools.get("pinNotInDispatch") or []
    if pin_not_in_dispatch:
        blockers.append(f"pin_not_in_dispatch={pin_not_in_dispatch}")
    if violations:
        blockers.append(f"quota_violations={len(violations)}")
    if mint_headroom_total < max(0, target_tickets - available_total):
        blockers.append(
            f"mint_headroom_total={mint_headroom_total} < tickets_needed={max(0, target_tickets - available_total)}"
        )
    return {
        "probe": "chrome_ticket_pool",
        "ts": utc_now(),
        "io_mode": "unified_snapshot" if snapshot is not None else "legacy_multi_ssh",
        "targets": {"concurrent": target_concurrent, "tickets": target_tickets},
        "readiness": {
            "ready_for_tickets": available_total >= target_tickets and not violations,
            "ready_for_concurrent": len(schedulable_runtime) >= target_concurrent and not violations,
            "ready_for_both": (
                available_total >= target_tickets
                and len(schedulable_runtime) >= target_concurrent
                and not violations
                and not orphans
                and not pin_not_in_dispatch
            ),
            "blockers": blockers,
        },
        "ticket_pool": {
            "by_status": by_status,
            "available_total": available_total,
            "ticket_holders": ticket_holders,
            "violations": violations,
        },
        "dispatch": {
            "dispatch_count": len(dispatch),
            "pin": pin,
            "runtime": runtime,
            "pin_not_in_dispatch": pin_not_in_dispatch,
            "selection_diagnostics": pools.get("selectionDiagnostics"),
            "four_pools_image": (pools.get("fourPools") or {}).get("image"),
        },
        "quota": quota,
        "mint_pipeline": {
            "worklist_count": len(work),
            "worklist_head": work[:15],
            "mint_headroom_total": mint_headroom_total,
            "tickets_needed": max(0, target_tickets - available_total),
        },
        "audit_rows": audit_rows,
        "pool_diff": (snapshot or {}).get("pool_diff"),
    }


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


def dispatch_mint_worklist(
    base_target: int,
    *,
    dispatch_ids: list[int] | None = None,
    workers: int | None = None,
    imagine_cache: dict[int, int] | None = None,
    pool_stats_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """实时调度池 + 额度/池深，返回待灌票工作项。"""
    dispatch = dispatch_ids or image_dispatch_account_ids()
    target = base_target if base_target > 0 else max(1, (10 + len(dispatch) - 1) // max(len(dispatch), 1))
    stats = pool_stats_snapshot or pool_stats()
    if imagine_cache is not None:
        imagine = {int(k): int(v) for k, v in imagine_cache.items()}
    else:
        imagine = imagine_remaining_by_account(dispatch, workers=workers)
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
        before_avail: int | None = None
        after_avail: int | None = None
        try:
            before_avail = pool_available(aid)
        except Exception as stats_exc:
            log_event("jit_mint_stats_warn", account_id=aid, stage="before", error=str(stats_exc)[:200])
        row = mint_ticket_row_from_sso(aid, sso_token, timeout=timeout)
        pushed = push_ticket(row, ttl_hours=ttl_hours)
        try:
            after_avail = pool_available(aid)
        except Exception as stats_exc:
            log_event("jit_mint_stats_warn", account_id=aid, stage="after", error=str(stats_exc)[:200])
        if before_avail is not None and after_avail is not None:
            pool_delta = after_avail - before_avail
        else:
            pool_delta = 1
        receipt = {
            "ok": True,
            "account_id": aid,
            "ticket_id": pushed.get("id"),
            "expires_at": pushed.get("expires_at"),
            "pool_before": before_avail,
            "pool_after": after_avail,
            "pool_delta": pool_delta,
            "meta_len": len(row.get("statsig_meta") or ""),
        }
        if pool_delta < 1:
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


def panda_image_concurrent_mixed(
    prompts: list[str],
    *,
    workers: int | None = None,
    log_since_s: int = 300,
) -> dict[str, Any]:
    """Panda 并发生图，每条请求使用不同 prompt（中英混合输入）。"""
    if not prompts:
        raise ValueError("prompts required")
    n = len(prompts)
    pool_workers = workers or n
    script = f"""
import concurrent.futures, json, subprocess, time, urllib.error, urllib.request
KEY=open({IMAGE_KEY_PATH!r}).read().strip()
BASE={PANDA_BASE!r}
PROMPTS={json.dumps(prompts, ensure_ascii=False)}
N={n}
WORKERS={pool_workers}

def one(i):
    prompt=PROMPTS[i % len(PROMPTS)]
    t0=time.time()
    body=json.dumps({{"model":"grok-imagine-image","prompt":prompt,"size":"1024x1024","n":1}}).encode()
    req=urllib.request.Request(
        BASE+"/v1/images/generations", data=body, method="POST",
        headers={{"Authorization":"Bearer "+KEY,"Content-Type":"application/json"}},
    )
    http=0; err=""; url=""
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            http=resp.status
            data=json.loads(resp.read().decode("utf-8","replace")).get("data") or []
            if data:
                url=(data[0].get("url") or "")[:120]
    except urllib.error.HTTPError as exc:
        http=exc.code
        err=exc.read().decode("utf-8","replace")[:300]
    return {{
        "idx": i + 1,
        "prompt": prompt[:80],
        "http": http,
        "wall_s": round(time.time() - t0, 1),
        "url": url,
        "error": err,
    }}

with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
    rows=list(ex.map(one, range(N)))
logs=subprocess.run(["docker","logs","grok2api","--since","{log_since_s}s"], capture_output=True, text=True)
text=logs.stdout or ""
pool_hits=text.count("chrome_ticket_pool_hit")
asset403=sum(1 for line in text.splitlines() if "web_lite_asset_download_failed" in line and "403" in line)
print(json.dumps({{"results":rows,"pool_hits":pool_hits,"asset403":asset403}}, ensure_ascii=False))
"""
    out = run_remote_python(script, remote_name="_chrome_ticket_image_concurrent_mixed.py", timeout=max(600, n * 200))
    line = out.splitlines()[-1]
    payload = json.loads(line)
    payload["ok"] = sum(1 for r in payload.get("results") or [] if r.get("http") == 200)
    payload["pool_hit"] = int(payload.get("pool_hits") or 0)
    return payload


def parse_image_logs_since(since_s: int = 90) -> dict[str, Any]:
    """解析 grok2api 日志中与 asset 下载/出口相关的行（用于判断 403 是否代理问题）。"""
    script = f"""
import json, re, subprocess
logs=subprocess.run(["docker","logs","grok2api","--since","{since_s}s"], capture_output=True, text=True)
text=logs.stdout or ""
asset_fails=[]
fallbacks=[]
pool_hits=0
egress_feedback=[]
for line in text.splitlines():
    if "chrome_ticket_pool_hit" in line:
        pool_hits += 1
    if "web_lite_asset_download_fallback" in line:
        m_scope=re.search(r'"from_scope"\\s*:\\s*"([^"]+)"', line)
        m_to=re.search(r'"to_scope"\\s*:\\s*"([^"]+)"', line)
        m_aid=re.search(r'"account_id"\\s*:\\s*(\\d+)', line)
        fallbacks.append({{
            "account_id": int(m_aid.group(1)) if m_aid else None,
            "from_scope": m_scope.group(1) if m_scope else None,
            "to_scope": m_to.group(1) if m_to else None,
        }})
    if "web_lite_asset_download_failed" in line:
        m_status=re.search(r'"status_code"\\s*:\\s*(\\d+)', line)
        m_scope=re.search(r'"scope"\\s*:\\s*"([^"]+)"', line)
        m_aid=re.search(r'"account_id"\\s*:\\s*(\\d+)', line)
        m_err=re.search(r'"error"\\s*:\\s*"([^"]*)"', line)
        asset_fails.append({{
            "account_id": int(m_aid.group(1)) if m_aid else None,
            "scope": m_scope.group(1) if m_scope else None,
            "status_code": int(m_status.group(1)) if m_status else None,
            "error": (m_err.group(1)[:120] if m_err else None),
        }})
    if "egress_feedback" in line or "egress_node" in line.lower():
        if "403" in line or "Forbidden" in line:
            egress_feedback.append(line[-240:])
asset403=sum(1 for x in asset_fails if x.get("status_code")==403)
print(json.dumps({{
    "pool_hits": pool_hits,
    "asset_fails": asset_fails[-20:],
    "asset403": asset403,
    "fallbacks": fallbacks[-10:],
    "egress_feedback": egress_feedback[-5:],
}}, ensure_ascii=False))
"""
    out = run_remote_python(script, remote_name="_parse_image_logs.py", timeout=45)
    return json.loads(out.splitlines()[-1])


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
