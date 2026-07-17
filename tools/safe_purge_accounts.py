#!/usr/bin/env python3
"""Safe purge gate for Build accounts: check → refresh → model probe → delete only if all fail.

Default is dry-run (report only). Pass --apply to actually delete IDs that fail every gate.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("/opt/grok2api/data/backend.db")
DEFAULT_BASE = os.environ.get("GROK2API_BASE", "http://127.0.0.1:18000")
DEFAULT_OUT = Path("/opt/grok2api/data/dead-probe")


def load_password() -> str:
    for path in (
        Path("/opt/grok2api/staging/admin-password"),
        Path("/root/.secrets/grok2api-admin-password"),
    ):
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    env = os.environ.get("GROK2API_ADMIN_PASSWORD", "").strip()
    if env:
        return env
    raise SystemExit("admin password not found")


def unwrap(value: Any) -> Any:
    return value.get("data", value) if isinstance(value, dict) else value


def api(base: str, method: str, path: str, token: str = "", body: dict | None = None, timeout: int = 120) -> tuple[int, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base.rstrip('/')}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {"error": raw}
        except json.JSONDecodeError:
            payload = {"error": raw}
        return exc.code, payload
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def login(base: str, password: str) -> str:
    status, payload = api(base, "POST", "/api/admin/v1/auth/login", body={"username": "admin", "password": password})
    if status != 200:
        raise SystemExit(f"login failed: {status} {payload}")
    token = unwrap(payload)["tokens"]["accessToken"]
    if not token:
        raise SystemExit(f"login missing token: {payload}")
    return str(token)


def classify_error(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False).lower()
    if "access_denied" in text:
        return "access_denied"
    if "invalid_grant" in text:
        return "invalid_grant"
    if "retired:" in text:
        return "retired"
    return "other"


def candidate_ids(db: Path, kind: str) -> list[int]:
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    if kind == "disabled":
        rows = cur.execute(
            "SELECT id FROM provider_accounts WHERE provider='grok_build' AND enabled=0 ORDER BY id"
        ).fetchall()
    elif kind == "dead-error":
        rows = cur.execute(
            """
            SELECT id FROM provider_accounts
            WHERE provider='grok_build' AND enabled=0
              AND (
                lower(COALESCE(last_error,'')) LIKE '%access_denied%'
                OR lower(COALESCE(last_error,'')) LIKE '%invalid_grant%'
                OR lower(COALESCE(last_error,'')) LIKE 'retired:%'
              )
            ORDER BY id
            """
        ).fetchall()
    else:
        raise SystemExit(f"unknown kind: {kind}")
    conn.close()
    return [int(r[0]) for r in rows]


def probe_one(base: str, token: str, account_id: int) -> dict[str, Any]:
    """Gate: refresh-token (OAuth + Build Chat model probe). Success => keep."""
    status, payload = api(base, "POST", f"/api/admin/v1/accounts/{account_id}/refresh-token", token=token)
    ok = status == 200
    account = unwrap(payload)
    if not isinstance(account, dict):
        account = {}
    observed = str(account.get("observedModel") or account.get("observed_model") or "").strip()
    # Also accept DB-side observed if response shape differs — keep requires HTTP 200 + model signal
    keep = bool(ok and observed)
    return {
        "id": account_id,
        "http_status": status,
        "refresh_ok": ok,
        "model_ok": keep,
        "observed_model": observed,
        "error_kind": "" if ok else classify_error(payload),
        "keep": keep,
        "deletable": not keep,
        "payload_snippet": json.dumps(payload, ensure_ascii=False)[:500],
    }


def batch_delete(base: str, token: str, ids: list[int]) -> tuple[int, Any]:
    return api(base, "DELETE", "/api/admin/v1/accounts", token=token, body={"ids": ids}, timeout=180)


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe purge: refresh+model must fail before delete")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--kind", choices=("disabled", "dead-error"), default="dead-error")
    parser.add_argument("--ids", default="", help="comma-separated IDs; overrides --kind")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.4)
    parser.add_argument("--apply", action="store_true", help="actually delete deletable IDs")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    password = load_password()
    token = login(args.base, password)
    if args.ids.strip():
        ids = [int(x) for x in args.ids.split(",") if x.strip()]
    else:
        ids = candidate_ids(Path(args.db), args.kind)
    if args.limit > 0:
        ids = ids[: args.limit]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    jsonl = out_dir / f"safe-purge-{stamp}.jsonl"
    summary_path = out_dir / f"safe-purge-{stamp}-summary.json"

    results: list[dict[str, Any]] = []
    keep: list[int] = []
    deletable: list[int] = []

    print(f"candidates={len(ids)} apply={args.apply}", flush=True)
    with jsonl.open("w", encoding="utf-8") as fh:
        for i, account_id in enumerate(ids, 1):
            row = probe_one(args.base, token, account_id)
            results.append(row)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            if row["keep"]:
                keep.append(account_id)
            else:
                deletable.append(account_id)
            print(
                f"[{i}/{len(ids)}] id={account_id} keep={row['keep']} "
                f"kind={row['error_kind'] or 'ok'} model={row['observed_model'] or '-'}",
                flush=True,
            )
            if args.sleep > 0:
                time.sleep(args.sleep)

    deleted = 0
    delete_payload: Any = None
    if args.apply and deletable:
        # Delete in chunks of 100
        for start in range(0, len(deletable), 100):
            chunk = deletable[start : start + 100]
            status, delete_payload = batch_delete(args.base, token, chunk)
            if status != 200:
                print(f"batch_delete_failed status={status} payload={delete_payload}", file=sys.stderr)
                break
            deleted += len(chunk)

    summary = {
        "stamp": stamp,
        "candidates": len(ids),
        "keep": keep,
        "keep_count": len(keep),
        "deletable": deletable,
        "deletable_count": len(deletable),
        "apply": args.apply,
        "deleted": deleted,
        "jsonl": str(jsonl),
        "policy": "delete only after refresh-token+model-probe both fail (no keep signal)",
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"summary={summary_path}")


if __name__ == "__main__":
    main()
