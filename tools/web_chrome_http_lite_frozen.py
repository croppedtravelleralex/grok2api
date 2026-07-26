#!/usr/bin/env python3
"""FROZEN: Chrome channel sign → curl_cffi HTTP chat/Lite.

Canonical doc: docs/http-reverse-lite-chain.md
Implementation: tools/_chrome_channel_chat_image.py (do not fork logic elsewhere)
Bench: tools/_chrome_lite_bench.py
SSO export: tools/panda_export_fresh_web_sso.py | tools/panda_export_free_web_sso.py

Do not evolve behavior here without updating the doc + acceptance note.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / ".tmp"


def _load_chrome():
    spec = importlib.util.spec_from_file_location(
        "chrome_http_lite", ROOT / "tools" / "_chrome_channel_chat_image.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "OPENSSL_CONF", "SSL_CERT_DIR"):
        os.environ.pop(k, None)

    parser = argparse.ArgumentParser(description="Frozen Chrome→HTTP Lite image canary")
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument(
        "--sso-file",
        type=Path,
        default=TMP / "web-sso-canary.json",
        help="SSO pool JSON from panda export",
    )
    parser.add_argument(
        "--prompt",
        default=(
            "a realistic tuxedo cat (black-and-white bicolor domestic cat / 奶牛猫), "
            "NOT a cow hybrid; white chest and paws, black body, clear fur detail"
        ),
        help="Lite prompt body without Drawing: prefix",
    )
    parser.add_argument("--skip-chat-probe", action="store_true", help="skip extra PONG after pick_sig")
    args = parser.parse_args()

    ch = _load_chrome()
    v1 = ch.v1
    accounts = v1.load_accounts(args.sso_file)
    acc = next((a for a in accounts if int(a["id"]) == args.account_id), None)
    if not acc:
        print(json.dumps({"event": "fail", "reason": f"account {args.account_id} not in {args.sso_file}"}, ensure_ascii=False))
        raise SystemExit(2)

    prompt = "Drawing: " + str(args.prompt).strip()
    ch.PROMPT = prompt
    out_dir = TMP / "lite-images" / f"frozen-{time.strftime('%Y%m%d-%H%M%S')}-a{args.account_id}"
    print(
        json.dumps(
            {
                "event": "start",
                "frozen": True,
                "account_id": args.account_id,
                "channel": "chrome",
                "ua": ch.UA,
                "impersonate": ch.HTTP_IMPERSONATE,
                "prompt": prompt,
                "out": str(out_dir),
                "doc": "docs/http-reverse-lite-chain.md",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    signed = ch.sign_with_chrome(acc["sso"])
    print(
        json.dumps(
            {
                "event": "signed",
                "source": signed.get("source"),
                "has_sig": bool(signed.get("statsigId")),
                "has_cf": "cf_clearance" in (signed.get("cookie") or ""),
                "candidates_n": len(signed.get("candidates") or []),
                "error": signed.get("error"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if not signed.get("statsigId") and not signed.get("candidates"):
        raise SystemExit(2)

    sig, cookie, probes = ch.pick_working_sig(acc["sso"], signed)
    print(json.dumps({"event": "probes", "rows": probes, "picked": bool(sig)}, ensure_ascii=False), flush=True)
    if not sig:
        raise SystemExit(2)

    chat = {"kind": "skipped"}
    if not args.skip_chat_probe:
        chat = v1.post_rest(
            acc["sso"],
            sig,
            v1.chat_payload("Reply with exactly: PONG"),
            cookie_override=cookie,
            user_agent=ch.UA,
        )
        print(
            json.dumps({"event": "http_chat", "http": chat.get("http"), "kind": chat.get("kind")}, ensure_ascii=False),
            flush=True,
        )

    lite = v1.post_rest(
        acc["sso"],
        sig,
        v1.chat_payload(prompt, enable_image=True),
        cookie_override=cookie,
        user_agent=ch.UA,
    )
    print(
        json.dumps(
            {
                "event": "http_lite",
                "http": lite.get("http"),
                "kind": lite.get("kind"),
                "n": lite.get("image_count"),
                "s": lite.get("elapsed_s"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    saved = []
    if lite.get("kind") == "image_ok" and lite.get("image_urls"):
        out_dir.mkdir(parents=True, exist_ok=True)
        for url in lite["image_urls"]:
            row = v1.download_asset(
                v1.absolute_asset_url(url),
                acc["sso"],
                cookie_override=cookie,
                user_agent=ch.UA,
                dest_dir=out_dir,
            )
            saved.append(row)
            print(
                json.dumps(
                    {"event": "saved", "file": row.get("file"), "bytes": row.get("bytes"), "http": row.get("http")},
                    ensure_ascii=False,
                ),
                flush=True,
            )

    chat_ok = args.skip_chat_probe or chat.get("kind") == "chat_ok"
    ok = chat_ok and lite.get("kind") == "image_ok"
    result_path = TMP / "web-chrome-http-lite-frozen-result.json"
    result_path.write_text(
        json.dumps(
            {
                "frozen": True,
                "account_id": args.account_id,
                "prompt": prompt,
                "probes": probes,
                "http_chat": {"http": chat.get("http"), "kind": chat.get("kind")},
                "http_lite": {
                    "http": lite.get("http"),
                    "kind": lite.get("kind"),
                    "image_count": lite.get("image_count"),
                },
                "saved": saved,
                "out": str(out_dir),
                "acceptance": {"ok": ok},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps({"event": "acceptance", "ok": ok, "path": str(result_path), "out": str(out_dir)}, ensure_ascii=False),
        flush=True,
    )
    raise SystemExit(0 if ok else 2)


if __name__ == "__main__":
    main()
