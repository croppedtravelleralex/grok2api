#!/usr/bin/env python3
"""Smoke test against a running grok-signer instance."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

META48 = bytes(range(48))
FP = "smokefingerprint"


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test grok-signer HTTP API")
    parser.add_argument("--base-url", default=os.environ.get("SIGNER_BASE_URL", "http://127.0.0.1:8788"))
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    for path in ("/healthz", "/readyz"):
        try:
            with urlopen(base + path, timeout=5) as resp:
                payload = json.loads(resp.read().decode())
            print(json.dumps({"path": path, "status": resp.status, "body": payload}, ensure_ascii=False))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            print(json.dumps({"path": path, "status": exc.code, "body": body}, ensure_ascii=False))
            return 1

    sign_body = json.dumps(
        {
            "method": "POST",
            "path": "/rest/rate-limits",
            "environment": {"metaContent": "html-meta-not-used"},
        }
    ).encode()
    req = Request(base + "/sign", data=sign_body, method="POST", headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=5) as resp:
        payload = json.loads(resp.read().decode())
    sig = payload.get("x-statsig-id", "")
    raw = base64.b64decode(sig + "=" * ((4 - len(sig) % 4) % 4))
    if len(raw) != 70:
        print(json.dumps({"path": "/sign", "error": "invalid statsig length", "len": len(raw)}))
        return 1
    print(json.dumps({"path": "/sign", "status": resp.status, "statsig_len": len(sig)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if os.environ.get("SIGNER_PAIR_FILE") or (os.environ.get("SIGNER_SEED_HEX") and os.environ.get("SIGNER_HEX")):
        pass
    else:
        print("warning: pair env not set; smoke test expects a running signer with pair configured", file=sys.stderr)
    sys.exit(main())
