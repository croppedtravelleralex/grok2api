#!/usr/bin/env python3
"""Local Windows probe: can we hit grok.com without Chrome bridge?"""
from __future__ import annotations

import json
import os
import sqlite3
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "backend.db"


def dump_local_accounts() -> None:
    if not DB.exists():
        print(f"db_missing={DB}")
        return
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    print("=== local provider counts ===")
    for row in con.execute(
        "SELECT provider, COUNT(*) AS c, SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS e "
        "FROM provider_accounts GROUP BY provider"
    ):
        print(dict(row))
    print("=== grok_web sample ===")
    for row in con.execute(
        "SELECT id, name, email, enabled, auth_status, substr(COALESCE(last_error,''),1,80) AS err "
        "FROM provider_accounts WHERE provider='grok_web' ORDER BY id LIMIT 15"
    ):
        print(dict(row))
    print("=== egress ===")
    for row in con.execute(
        "SELECT id, name, enabled, substr(COALESCE(user_agent,''),1,40) AS ua, "
        "length(COALESCE(encrypted_cloudflare_cookie,'')) AS cf_len FROM egress_nodes LIMIT 10"
    ):
        print(dict(row))
    con.close()


def fetch(url: str, headers: dict | None = None, timeout: int = 25) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
            return resp.status, dict(resp.headers), resp.read(2000)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read(2000)


def probe_homepage() -> None:
    print("=== urllib homepage ===")
    print("getproxies", urllib.request.getproxies())
    # Direct (no system proxy)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request("https://grok.com/", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        })
        with opener.open(req, timeout=15) as resp:
            print("direct status", resp.status)
    except Exception as exc:  # noqa: BLE001
        print("direct FAIL", type(exc).__name__, str(exc)[:120])
    status, headers, body = fetch("https://grok.com/")
    text = body.decode("utf-8", "replace")
    print("system_proxy status", status)
    print("cf-mitigated", headers.get("cf-mitigated") or headers.get("Cf-Mitigated"))
    print("server", headers.get("Server") or headers.get("server"))
    print("has_title_grok", "<title>Grok</title>" in text or ">Grok<" in text[:500])
    print("body_prefix", text[:180].replace("\n", " "))


def probe_rest_without_auth() -> None:
    print("=== REST without SSO (expect 401/403) ===")
    for path in (
        "/rest/app-chat/conversations/new",
        "/rest/rate-limits",
    ):
        status, headers, body = fetch(
            "https://grok.com" + path,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://grok.com",
                "Referer": "https://grok.com/",
            },
            timeout=20,
        )
        print(path, "status", status, "cf", headers.get("cf-mitigated"), "body", body[:120])


def probe_curl_cffi() -> None:
    print("=== curl_cffi via 127.0.0.1:7897 ===")
    try:
        from curl_cffi import requests as crequests
    except Exception as exc:  # noqa: BLE001
        print("curl_cffi_import_failed", exc)
        return
    proxies = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
    for impersonate in ("chrome131", "chrome124", "chrome"):
        try:
            r = crequests.get("https://grok.com/", impersonate=impersonate, timeout=20, proxies=proxies)
            print(impersonate, "status", r.status_code, "cf", r.headers.get("cf-mitigated"), "len", len(r.text))
            r2 = crequests.post(
                "https://grok.com/rest/app-chat/conversations/new",
                impersonate=impersonate, timeout=20, proxies=proxies,
                headers={"content-type": "application/json", "origin": "https://grok.com", "referer": "https://grok.com/"},
                data="{}",
            )
            print(impersonate, "rest", r2.status_code, r2.text[:160])
            return
        except Exception as exc:  # noqa: BLE001
            print(impersonate, "error", type(exc).__name__, str(exc)[:160])


def main() -> None:
    dump_local_accounts()
    probe_homepage()
    probe_rest_without_auth()
    probe_curl_cffi()


if __name__ == "__main__":
    main()
