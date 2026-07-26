#!/usr/bin/env python3
"""On Panda: export one grok_web SSO token to stdout (ephemeral JIT minting)."""
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
    raise SystemExit("credentialEncryptionKey missing")


def decrypt(key: bytes, blob: str) -> str:
    raw = base64.b64decode(blob)
    nonce, ciphertext = raw[:12], raw[12:]
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip().isdigit():
        print(json.dumps({"error": "usage: panda_export_web_sso_one.py <account_id>"}))
        raise SystemExit(2)
    account_id = int(sys.argv[1])
    row = sqlite3.connect(DB).execute(
        """
        SELECT a.id, a.name, a.email, a.enabled, a.auth_status, c.encrypted_primary AS token
        FROM provider_accounts a
        JOIN account_credentials c ON c.account_id = a.id
        WHERE a.id=? AND a.provider='grok_web' AND c.encrypted_primary <> ''
        """,
        (account_id,),
    ).fetchone()
    if not row:
        print(json.dumps({"account_id": account_id, "error": "sso_not_found"}))
        raise SystemExit(1)
    try:
        token = decrypt(load_key(), row[5]).strip()
        if token.lower().startswith("sso="):
            token = token[4:].strip()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"account_id": account_id, "error": str(exc)[:120]}))
        raise SystemExit(1)
    if len(token) < 20:
        print(json.dumps({"account_id": account_id, "error": "sso_too_short"}))
        raise SystemExit(1)
    print(
        json.dumps(
            {
                "account_id": account_id,
                "name": row[1] or "",
                "email": row[2] or "",
                "enabled": bool(row[3]),
                "auth_status": row[4] or "",
                "token_len": len(token),
                "token_prefix": token[:6],
                "sso": token,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
