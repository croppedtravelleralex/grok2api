"""Load matched meta48 + fingerprint pair from env (never log secrets)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from statsig import b64decode


@dataclass(frozen=True)
class SignerPair:
    meta48: bytes
    fingerprint: str
    trailer: bytes = b"\x03"


class PairLoadError(RuntimeError):
    pass


def _meta_from_seed_hex(seed_hex: str) -> bytes:
    seed_hex = seed_hex.strip().lower()
    if seed_hex.startswith("0x"):
        seed_hex = seed_hex[2:]
    if len(seed_hex) != 96:
        raise PairLoadError("SIGNER_SEED_HEX must be 96 hex chars (48 bytes)")
    try:
        meta = bytes.fromhex(seed_hex)
    except ValueError as exc:
        raise PairLoadError("SIGNER_SEED_HEX is not valid hex") from exc
    if len(meta) != 48:
        raise PairLoadError("SIGNER_SEED_HEX decoded length must be 48 bytes")
    return meta


def _pair_from_mapping(data: dict) -> SignerPair:
    meta_raw = data.get("meta_b64") or data.get("meta") or data.get("verification_b64")
    fp = data.get("fingerprint") or data.get("fp")
    if not meta_raw or not fp:
        raise PairLoadError("pair file must include meta_b64 and fingerprint")
    meta = b64decode(str(meta_raw))
    if len(meta) != 48:
        raise PairLoadError(f"meta must decode to 48 bytes, got {len(meta)}")
    trailer_hex = str(data.get("trailer_hex") or "03").strip()
    try:
        trailer = bytes.fromhex(trailer_hex)
    except ValueError as exc:
        raise PairLoadError("trailer_hex is not valid hex") from exc
    if len(trailer) != 1:
        raise PairLoadError("trailer_hex must be one byte")
    return SignerPair(meta48=meta, fingerprint=str(fp), trailer=trailer)


def load_pair() -> SignerPair:
    pair_file = os.environ.get("SIGNER_PAIR_FILE", "").strip()
    if pair_file:
        path = Path(pair_file)
        if not path.is_file():
            raise PairLoadError(f"SIGNER_PAIR_FILE not found: {pair_file}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PairLoadError("SIGNER_PAIR_FILE is not valid JSON") from exc
        if not isinstance(data, dict):
            raise PairLoadError("SIGNER_PAIR_FILE root must be a JSON object")
        return _pair_from_mapping(data)

    seed_hex = os.environ.get("SIGNER_SEED_HEX", "").strip()
    fp_hex = os.environ.get("SIGNER_HEX", "").strip()
    if seed_hex and fp_hex:
        return SignerPair(meta48=_meta_from_seed_hex(seed_hex), fingerprint=fp_hex)

    raise PairLoadError("set SIGNER_PAIR_FILE or SIGNER_SEED_HEX + SIGNER_HEX")
