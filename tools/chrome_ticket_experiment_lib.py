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
    proc = ssh_run("python3 /tmp/_panda_admin_token.py", timeout=90)
    if proc.returncode != 0:
        raise RuntimeError(f"admin token failed: {(proc.stderr or proc.stdout)[:300]}")
    return proc.stdout.strip()


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


def pool_available(account_id: int | None = None) -> int:
    stats = pool_stats()
    by_status = stats.get("ByStatus") or stats.get("byStatus") or {}
    if account_id is None:
        return int(by_status.get("available") or 0)
    for row in stats.get("AvailableByAccount") or stats.get("availableByAccount") or []:
        if int(row.get("AccountID") or row.get("accountId") or row.get("account_id") or 0) == account_id:
            return int(row.get("Count") or row.get("count") or 0)
    return 0


def imagine_generations(remaining: int, total: int) -> int | None:
    """Align with Go account.ImagineGenerations (micro-credits → 生图次数)."""
    if total <= 0 or remaining < 0:
        return None
    if total <= 1000:
        return remaining
    unit = max(1, total // 10)
    return (remaining + unit - 1) // unit


def imagine_remaining_by_account(account_ids: list[int] | None = None) -> dict[int, int]:
    """Read Imagine **generation count** remaining per account (not raw micro-credits)."""
    token = admin_token()
    proc = ssh_run(
        f"curl -fsS '{PANDA_BASE}/api/admin/v1/accounts?provider=grok_web&status=enabled&pageSize=500' "
        f"-H 'Authorization: Bearer {token}'",
        timeout=90,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"account list failed: {(proc.stderr or proc.stdout)[:300]}")
    payload = json.loads(proc.stdout)
    items = (payload.get("data") or {}).get("items") or []
    wanted = set(account_ids or [])
    out: dict[int, int] = {}
    for acc in items:
        aid = int(acc.get("id") or 0)
        if not aid or (wanted and aid not in wanted):
            continue
        for window in acc.get("quotaWindows") or []:
            if window.get("mode") != "imagine":
                continue
            total = int(window.get("total") or 0)
            remaining = int(window.get("remaining") or 0)
            gens = imagine_generations(remaining, total)
            if gens is not None:
                out[aid] = max(0, gens)
            break
    return out


def effective_mint_target(account_id: int, base_target: int) -> int:
    """票池深度上限 = min(base_target, Imagine remaining)。无额度则不灌票。"""
    imagine = imagine_remaining_by_account([account_id]).get(account_id, 0)
    if imagine <= 0:
        return 0
    return min(base_target, imagine)


def mint_headroom(account_id: int, base_target: int) -> int:
    """距离目标池深还差多少张（已考虑额度上限）。"""
    target = effective_mint_target(account_id, base_target)
    if target <= 0:
        return 0
    return max(0, target - pool_available(account_id))


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


def mint_ticket_row(account_id: int, sso_file: Path, *, timeout: int = 120) -> dict[str, Any]:
    chrome, v1 = load_chrome_modules()
    accounts = load_accounts(sso_file)
    acc = accounts.get(account_id)
    if not acc:
        raise RuntimeError(f"account {account_id} missing in {sso_file}")
    signed = chrome.sign_with_chrome(acc["sso"], timeout_s=timeout, proxy=v1.PROXY)
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
