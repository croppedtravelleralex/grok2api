#!/usr/bin/env python3
"""On panda: export grok_web SSO, optionally excluding burned IDs / preferring non-production."""
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
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    exclude = {int(x) for x in (sys.argv[2] if len(sys.argv) > 2 else "").split(",") if x.strip().isdigit()}
    mode = (sys.argv[3] if len(sys.argv) > 3 else "fresh").strip()  # fresh|any|disabled
    key = load_key()
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row

    where = "a.provider='grok_web' AND c.encrypted_primary <> ''"
    if mode == "fresh":
        where += " AND a.enabled=1 AND a.auth_status='active'"
    elif mode == "disabled":
        where += " AND (a.enabled=0 OR a.auth_status!='active')"
    # any: no extra filter

    rows = con.execute(
        f"""
        SELECT a.id, a.name, a.email, a.enabled, a.auth_status, a.cooldown_until,
               substr(coalesce(a.last_error,''),1,80) AS err,
               c.encrypted_primary AS token
        FROM provider_accounts a
        JOIN account_credentials c ON c.account_id = a.id
        WHERE {where}
        ORDER BY a.id DESC
        """
    ).fetchall()

    out = []
    skipped = 0
    for row in rows:
        if int(row["id"]) in exclude:
            skipped += 1
            continue
        try:
            token = decrypt(key, row["token"]).strip()
            if token.lower().startswith("sso="):
                token = token[4:].strip()
        except Exception as exc:  # noqa: BLE001
            continue
        if len(token) < 20:
            continue
        out.append(
            {
                "id": row["id"],
                "name": row["name"] or "",
                "email": row["email"] or "",
                "enabled": bool(row["enabled"]),
                "auth_status": row["auth_status"],
                "token_len": len(token),
                "token_prefix": token[:6],
                "sso": token,
                "err": row["err"] or "",
            }
        )
        if len(out) >= limit:
            break

    print(
        json.dumps(
            {"count": len(out), "mode": mode, "excluded": len(exclude), "skipped_excluded": skipped, "accounts": out},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
