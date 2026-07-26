#!/usr/bin/env python3
"""Sign helper for Rust canary — frozen v1 playwright_sign + isolated verify.

Playwright and curl_cffi must not share one Windows process (OpenSSL clash).
Each attempt: subprocess(sign) → subprocess(post_worker verify).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent


def load_v1():
    path = ROOT / "web_http_chat_image_canary.v1.py"
    spec = importlib.util.spec_from_file_location("web_http_chat_image_canary_v1", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sign_in_subprocess(python: str, sso: str, timeout_s: int = 90) -> dict:
    """Run playwright_sign in a fresh interpreter (isolates Chromium OpenSSL)."""
    code = r"""
import json, sys
from pathlib import Path
import importlib.util
root = Path(r""" + json.dumps(str(ROOT)) + r""")
path = root / "web_http_chat_image_canary.v1.py"
spec = importlib.util.spec_from_file_location("v1", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
sso = sys.argv[1]
timeout = int(sys.argv[2])
out = mod.playwright_sign(sso, mod.CHAT_PATH, timeout_s=timeout)
# drop huge notes noise
print(json.dumps({
  "statsigId": out.get("statsigId"),
  "cookie": out.get("cookie"),
  "source": out.get("source"),
  "error": out.get("error"),
  "notes": (out.get("notes") or [])[:12],
  "cookie_names": out.get("cookie_names"),
}, ensure_ascii=False))
"""
    proc = subprocess.run(
        [python, "-c", code, sso, str(timeout_s)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=timeout_s + 60,
        check=False,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        return {
            "statsigId": None,
            "error": f"sign_subprocess_rc={proc.returncode}:{(proc.stderr or '')[:240]}",
        }
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    if not lines:
        return {"statsigId": None, "error": f"no_json_stdout:{(proc.stderr or '')[:200]}"}
    return json.loads(lines[-1])


def verify_in_subprocess(python: str, cookie: str, statsig: str, payload: dict, want_image: bool) -> dict:
    worker = ROOT / "web_http_post_worker.py"
    req = json.dumps(
        {"cookie": cookie, "statsig": statsig, "payload": payload, "want_image": want_image},
        ensure_ascii=False,
    )
    proc = subprocess.run(
        [python, str(worker)],
        input=req,
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=150,
        check=False,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        return {"kind": "worker_fail", "error": (proc.stderr or "")[:200], "http": 0}
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    if not lines:
        return {"kind": "worker_no_json", "error": (proc.stderr or "")[:200], "http": 0}
    return json.loads(lines[-1])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sso-file", type=Path, required=True)
    parser.add_argument("--account-id", type=int)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--verify-chat", action="store_true", default=True)
    parser.add_argument("--no-verify-chat", action="store_true")
    parser.add_argument("--verify-image", action="store_true")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--attempts", type=int, default=6)
    args = parser.parse_args()
    verify = (args.verify_chat and not args.no_verify_chat) or args.verify_image

    v1 = load_v1()
    accounts = v1.load_accounts(args.sso_file)
    if args.account_id:
        accounts = [a for a in accounts if a["id"] == args.account_id]
    if not accounts:
        raise SystemExit("no accounts")
    acc = accounts[0]

    last: dict | None = None
    for attempt in range(1, args.attempts + 1):
        signed = sign_in_subprocess(args.python, acc["sso"], timeout_s=90)
        source = str(signed.get("source") or "")
        if signed.get("statsigId") and (
            "rate-limit" in source
            or "/rest/media" in source
            or "/rest/products" in source
            or source.endswith(":products")
        ):
            signed["error"] = f"rejected non-chat sig source={source}"
            signed["statsigId"] = None
        last = signed
        if not signed.get("statsigId"):
            time.sleep(1.5)
            continue
        if not verify:
            break

        cookie = signed.get("cookie") or v1.cookie_header(acc["sso"])
        if args.verify_image:
            payload = v1.chat_payload(
                "Drawing: a simple red apple on white background, minimal",
                enable_image=True,
            )
            want_image = True
        else:
            payload = v1.chat_payload("Reply with exactly: PONG")
            want_image = False
        probe = verify_in_subprocess(args.python, cookie, signed["statsigId"], payload, want_image)
        ok = (
            (probe.get("kind") == "image_ok") or ((probe.get("image_count") or 0) > 0)
            if want_image
            else probe.get("kind") == "chat_ok"
        )
        signed["verify"] = {
            "http": probe.get("http"),
            "kind": probe.get("kind"),
            "image_count": probe.get("image_count"),
            "image_urls_prefix": probe.get("image_urls_prefix"),
            "attempt": attempt,
            "mode": "image" if want_image else "chat",
            "body_prefix": (probe.get("body_prefix") or "")[:160],
        }
        last = signed
        if ok:
            break
        signed["error"] = f"verify_failed:{probe.get('kind')}"
        signed["statsigId"] = None
        last = signed
        time.sleep(1.5)

    out = {
        "account_id": acc["id"],
        "statsigId": (last or {}).get("statsigId"),
        "cookie": (last or {}).get("cookie"),
        "source": (last or {}).get("source"),
        "error": (last or {}).get("error"),
        "notes": ((last or {}).get("notes") or [])[:12],
        "statsig_len": len((last or {}).get("statsigId") or ""),
        "cookie_names": (last or {}).get("cookie_names"),
        "verify": (last or {}).get("verify"),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(
        json.dumps(
            {
                "event": "sign_helper",
                "account_id": out["account_id"],
                "has_statsig": bool(out["statsigId"]),
                "statsig_len": out["statsig_len"],
                "source": out["source"],
                "error": out["error"],
                "verify": out.get("verify"),
                "out": str(args.out),
            },
            ensure_ascii=False,
        )
    )
    if not out["statsigId"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
