#!/usr/bin/env python3
"""On panda: export a few enabled Web SSO tokens for local HTTP canary (stdout JSON only)."""
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
    # format used by grok2api security.Cipher: nonce|ciphertext
    nonce, ciphertext = raw[:12], raw[12:]
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    key = load_key()
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT a.id, a.name, a.email, a.enabled, a.auth_status,
               c.encrypted_primary AS token
        FROM provider_accounts a
        JOIN account_credentials c ON c.account_id = a.id
        WHERE a.provider='grok_web' AND a.enabled=1 AND a.auth_status='active'
          AND c.encrypted_primary <> ''
        ORDER BY a.id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out = []
    for row in rows:
        try:
            token = decrypt(key, row["token"]).strip()
            if token.lower().startswith("sso="):
                token = token[4:].strip()
        except Exception as exc:  # noqa: BLE001
            out.append({"id": row["id"], "error": str(exc)[:80]})
            continue
        out.append({
            "id": row["id"],
            "name": row["name"] or "",
            "email": row["email"] or "",
            "token_len": len(token),
            "token_prefix": token[:6],
            "sso": token,
        })
    print(json.dumps({"count": len(out), "accounts": out}, ensure_ascii=False))


if __name__ == "__main__":
    main()
