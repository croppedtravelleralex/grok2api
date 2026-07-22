"""Pure-Python x-statsig-id generation (Twitter-family XOR/SHA256 layout)."""
from __future__ import annotations

import base64
import hashlib
import random
import struct
import time

EPOCH = 1682924400


def b64decode(value: str) -> bytes:
    pad = "=" * ((4 - len(value) % 4) % 4)
    return base64.b64decode(value + pad)


def generate_statsig(
    method: str,
    path: str,
    meta48: bytes,
    fingerprint: str,
    *,
    n: int | None = None,
    key: int | None = None,
    trailer: bytes = b"\x03",
) -> str:
    if len(meta48) != 48:
        raise ValueError(f"meta48 len={len(meta48)}")
    n = int(time.time() - EPOCH) if n is None else n
    digest = f"{method}!{path}!{n}obfiowerehiring{fingerprint}"
    sha = hashlib.sha256(digest.encode()).digest()[:16]
    key = random.randint(0, 255) if key is None else key
    block = meta48 + struct.pack("<I", n) + sha + trailer
    enc = bytearray([key]) + bytearray(b ^ key for b in block)
    return base64.b64encode(bytes(enc)).decode().rstrip("=")


def valid_statsig_id(value: str) -> bool:
    value = (value or "").strip()
    if not value:
        return False
    pad = "=" * ((4 - len(value) % 4) % 4)
    try:
        raw = base64.b64decode(value + pad)
    except Exception:
        return False
    return len(raw) == 70
