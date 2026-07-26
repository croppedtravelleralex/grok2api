#!/usr/bin/env python3
"""PoC: mint a Chrome-ticket-equivalent device cookie over pure HTTP (no browser).

Runs on Panda. Reuses panda_lite_with_ticket.py for SSO/egress decryption, then
performs a single impersonated homepage GET through the grok_web egress and
harvests the device cookies that a Chrome capture would otherwise provide.

Answers two questions:
  1. does a pure-HTTP authenticated GET yield grok_device_id?
  2. does it also yield x-userid, or must that come from elsewhere?

Output: one JSON object on stdout, ready to feed panda_lite_with_ticket.py.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

TOOLS_DIR = Path("/opt/grok2api/tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import panda_lite_with_ticket as lite  # noqa: E402
from curl_cffi import requests as crequests  # noqa: E402

GROK_BASE = "https://grok.com"
# Cookies a Chrome-minted ticket carries that actually reach grok.com.
DEVICE_COOKIE_NAMES = ("grok_device_id", "x-userid", "x-anonuserid")


def harvest(account_id: int, impersonate: str, timeout: int) -> dict:
    sso = lite.decrypt_sso(account_id)
    proxy, ua, cf = lite.load_egress("grok_web")
    cookie = lite.merge_cookie(sso, "", cf)

    session = crequests.Session()
    started = time.time()
    response = session.get(
        GROK_BASE + "/",
        headers={
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "user-agent": ua,
            "cookie": cookie,
        },
        proxies={"http": proxy, "https": proxy},
        impersonate=impersonate,
        timeout=timeout,
        allow_redirects=True,
    )

    jar = dict(session.cookies.items())
    device_bits = [f"{name}={jar[name]}" for name in DEVICE_COOKIE_NAMES if jar.get(name)]
    meta = lite.extract_meta_from_html(response.text or "")

    return {
        "account_id": account_id,
        "http": response.status_code,
        "elapsed_s": round(time.time() - started, 2),
        "user_agent": ua,
        "jar_names": sorted(jar),
        "device_cookie": "; ".join(device_bits),
        "has_grok_device_id": bool(jar.get("grok_device_id")),
        "has_x_userid": bool(jar.get("x-userid")),
        "statsig_meta": meta or "",
        "meta_len": len(meta or ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--impersonate", default="chrome146")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--out", type=Path, help="also write the JSON here")
    args = parser.parse_args()

    result = harvest(args.account_id, args.impersonate, args.timeout)
    payload = json.dumps(result, ensure_ascii=False)
    if args.out:
        args.out.write_text(payload, encoding="utf-8")
    print(payload)
    return 0 if result["has_grok_device_id"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
