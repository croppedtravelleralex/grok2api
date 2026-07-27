#!/usr/bin/env python3
"""对照 free-usage-gates 与 Lite 探针，审计账号能否 Lite 生图（Panda 本机执行）。

用法:
  ssh panda 'python3 -' < tools/live_imagine_quota_audit.py
  PROBE_SAMPLE=10 ssh panda 'python3 -' < tools/live_imagine_quota_audit.py

不 scp 文件；stdin 喂给 Panda python3。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

DB = "/opt/grok2api/data/backend.db"
SAMPLE = int(os.environ.get("PROBE_SAMPLE", "8"))
MUST = [int(x) for x in os.environ.get("PROBE_MUST", "86,1382,373").split(",") if x.strip()]


def pick_ids() -> list[int]:
    conn = sqlite3.connect(DB)
    random_ids = [
        r[0]
        for r in conn.execute(
            """
            SELECT id FROM provider_accounts
            WHERE provider='grok_web' AND enabled=1
            ORDER BY RANDOM() LIMIT ?
            """,
            (SAMPLE,),
        ).fetchall()
    ]
    conn.close()
    out: list[int] = []
    for x in MUST + random_ids:
        if x not in out:
            out.append(x)
    return out


def gate_imagine(account_id: int) -> dict:
    sys.path.insert(0, "/opt/grok2api/tools")
    import panda_lite_with_ticket as lite  # noqa: WPS433
    from curl_cffi import requests as creq

    sso = lite.decrypt_sso(account_id)
    proxy_url, ua, cf = lite.load_egress("grok_web")
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    cookie = lite.merge_cookie(sso, "", cf)
    r = creq.get(
        "https://grok.com/rest/usage/free-usage-gates",
        impersonate="chrome131",
        proxies=proxies,
        headers={"User-Agent": ua, "Cookie": cookie},
        timeout=60,
    )
    data = r.json().get("imagine") or {}
    allowance = int(str(data.get("allowance") or "0") or 0)
    remaining = int(str(data.get("remaining") or "0") or 0)
    known = allowance > 0 or remaining > 0
    if allowance > 0 and remaining > 0:
        gens = remaining if allowance <= 1000 else (remaining + max(1, allowance // 10) - 1) // max(1, allowance // 10)
    elif allowance > 0 and remaining <= 0:
        gens = 0
        known = True
    else:
        gens = None
    return {"allowance": allowance, "remaining": remaining, "gens_known": known, "gens": gens}


def lite_probe(account_id: int) -> dict:
    payload = json.dumps({"account_id": account_id, "prompt": "a tiny red dot"})
    proc = subprocess.run(
        ["python3", "/opt/grok2api/tools/panda_lite_with_ticket.py"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=150,
    )
    out = proc.stdout + proc.stderr
    http = None
    ok = '"ok": true' in out
    for line in out.splitlines():
        if '"event": "panda_lite"' in line:
            try:
                row = json.loads(line)
                http = row.get("http")
            except json.JSONDecodeError:
                pass
    return {"http": http, "ok": ok}


def model_status(account_id: int) -> str:
    conn = sqlite3.connect(DB)
    row = conn.execute(
        """
        SELECT status FROM account_model_states
        WHERE account_id=? AND upstream_model='grok-imagine-image'
        """,
        (account_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else "missing"


def main() -> None:
    ids = pick_ids()
    print("account_id gate_rem gate_tot gens_known gens lite_http lite_ok model_status")
    for aid in ids:
        try:
            gate = gate_imagine(aid)
            lite = lite_probe(aid)
            status = model_status(aid)
            gens = gate["gens"] if gate["gens_known"] else "?"
            print(
                f"{aid} {gate['remaining']} {gate['allowance']} {gate['gens_known']} {gens} "
                f"{lite.get('http')} {lite.get('ok')} {status}"
            )
        except Exception as exc:
            print(f"{aid} ERR {type(exc).__name__}: {str(exc)[:120]}")


if __name__ == "__main__":
    main()
