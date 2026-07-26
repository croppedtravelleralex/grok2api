#!/usr/bin/env python3
"""Export grok_web SSO that look like free/basic by stored quota windows (fast=30 or auto=7/20)."""
from __future__ import annotations

import base64
import json
import sqlite3
import sys
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


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    exclude = {int(x) for x in (sys.argv[2] if len(sys.argv) > 2 else "").split(",") if x.strip().isdigit()}
    key = load_key()
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row

    # Prefer accounts whose last synced windows look Basic/free.
    free_ids = [
        int(r["id"])
        for r in con.execute(
            """
            SELECT DISTINCT a.id AS id
            FROM provider_accounts a
            JOIN account_quota_windows w ON w.account_id = a.id
            WHERE a.provider='grok_web' AND a.enabled=1 AND a.auth_status='active'
              AND (
                (w.mode='fast' AND w.total=30)
                OR (w.mode='auto' AND w.total IN (7, 20))
              )
            ORDER BY a.id DESC
            """
        ).fetchall()
    ]
    # Fallback: all active web if no quota rows
    if not free_ids:
        free_ids = [
            int(r["id"])
            for r in con.execute(
                """
                SELECT id FROM provider_accounts
                WHERE provider='grok_web' AND enabled=1 AND auth_status='active'
                ORDER BY id DESC LIMIT 50
                """
            ).fetchall()
        ]

    out = []
    for aid in free_ids:
        if aid in exclude:
            continue
        row = con.execute(
            """
            SELECT a.id, a.name, a.email, a.enabled, a.auth_status,
                   c.encrypted_primary AS token
            FROM provider_accounts a
            JOIN account_credentials c ON c.account_id=a.id
            WHERE a.id=? AND c.encrypted_primary<>''
            """,
            (aid,),
        ).fetchone()
        if not row:
            continue
        wins = [
            dict(w)
            for w in con.execute(
                """
                SELECT mode, remaining, total, window_seconds
                FROM account_quota_windows WHERE account_id=?
                """,
                (aid,),
            ).fetchall()
        ]
        try:
            token = decrypt(key, row["token"]).strip()
            if token.lower().startswith("sso="):
                token = token[4:].strip()
        except Exception:
            continue
        out.append(
            {
                "id": aid,
                "name": row["name"] or "",
                "email": row["email"] or "",
                "enabled": True,
                "auth_status": row["auth_status"],
                "tier_hint": "free/basic",
                "quota_windows": wins,
                "token_len": len(token),
                "token_prefix": token[:6],
                "sso": token,
            }
        )
        if len(out) >= limit:
            break

    print(json.dumps({"count": len(out), "mode": "free/basic", "accounts": out}, ensure_ascii=False))


if __name__ == "__main__":
    main()
