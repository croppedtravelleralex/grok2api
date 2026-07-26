#!/usr/bin/env python3
"""On panda: export grok_web SSO from resting pools (cooldown/recovery/quarantine), not production."""
from __future__ import annotations

import base64
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DB = Path("/opt/grok2api/data/backend.db")
CONFIG = Path("/opt/grok2api/config.yaml")


def load_key() -> bytes:
    text = CONFIG.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "credentialEncryptionKey" in line:
            raw = line.split(":", 1)[1].strip().strip('"').strip("'")
            return base64.b64decode(raw)
    raise SystemExit("encryption key missing")


def decrypt(key: bytes, blob: str) -> str:
    raw = base64.b64decode(blob)
    nonce, ciphertext = raw[:12], raw[12:]
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def classify(row: sqlite3.Row, in_recovery: bool, quota_exhausted: bool, now: datetime) -> str:
    enabled = bool(row["enabled"])
    auth = row["auth_status"] or ""
    err = (row["last_error"] or "").lower()
    cooldown = row["cooldown_until"]
    if not enabled:
        if err.startswith("retired:") or "retired" in err:
            return "retired"
        return "disabled"
    if auth == "reauthRequired":
        return "quarantine"
    if in_recovery:
        return "recovery"
    if quota_exhausted:
        return "recovery"
    if cooldown:
        try:
            # sqlite may return str
            cd = cooldown
            if isinstance(cd, str):
                cd = datetime.fromisoformat(cd.replace("Z", ""))
            if cd > now:
                return "cooldown"
        except Exception:
            pass
    # crude verification: active but never observed / empty token usage markers
    return "production"


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    pools_want = set((sys.argv[2] if len(sys.argv) > 2 else "cooldown,recovery,quarantine").split(","))
    key = load_key()
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    now = now_utc()

    recovery_ids = {
        r["account_id"]
        for r in con.execute(
            "SELECT account_id FROM account_quota_recovery WHERE status IN ('exhausted','probing','waiting_reset','waitingReset')"
        ).fetchall()
    }
    # also check model quota blocks still cooling
    blocked = {
        r["account_id"]
        for r in con.execute(
            "SELECT DISTINCT account_id FROM account_model_quota_blocks WHERE cooldown_until > ?",
            (now.isoformat(sep=" "),),
        ).fetchall()
    }

    rows = con.execute(
        """
        SELECT a.id, a.name, a.email, a.enabled, a.auth_status, a.cooldown_until, a.last_error,
               c.encrypted_primary AS token
        FROM provider_accounts a
        JOIN account_credentials c ON c.account_id = a.id
        WHERE a.provider='grok_web' AND c.encrypted_primary <> ''
        ORDER BY a.id ASC
        """
    ).fetchall()

    pool_counts: dict[str, int] = {}
    out = []
    for row in rows:
        pool = classify(row, row["id"] in recovery_ids, row["id"] in blocked, now)
        pool_counts[pool] = pool_counts.get(pool, 0) + 1
        if pool not in pools_want:
            continue
        try:
            token = decrypt(key, row["token"]).strip()
            if token.lower().startswith("sso="):
                token = token[4:].strip()
        except Exception as exc:  # noqa: BLE001
            out.append({"id": row["id"], "pool": pool, "error": str(exc)[:80]})
            continue
        if len(token) < 20:
            continue
        out.append(
            {
                "id": row["id"],
                "name": row["name"] or "",
                "email": row["email"] or "",
                "pool": pool,
                "auth_status": row["auth_status"],
                "enabled": bool(row["enabled"]),
                "token_len": len(token),
                "token_prefix": token[:6],
                "sso": token,
                "err": (row["last_error"] or "")[:80],
            }
        )
        if len([x for x in out if x.get("sso")]) >= limit:
            break

    good = [a for a in out if a.get("sso")]
    print(
        json.dumps(
            {
                "count": len(good),
                "pool_counts": pool_counts,
                "want": sorted(pools_want),
                "accounts": good,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
