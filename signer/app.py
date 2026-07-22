#!/usr/bin/env python3
"""Grok Signer sidecar: POST /sign compatible with backend statsig.go."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from pair import PairLoadError, SignerPair, load_pair
from statsig import generate_statsig, valid_statsig_id

LOG = logging.getLogger("grok-signer")
PORT = int(os.environ.get("SIGNER_PORT", "8788"))
PROBE_PATH = os.environ.get("SIGNER_PROBE_PATH", "/rest/rate-limits")
PROBE_BASE_URL = os.environ.get("SIGNER_PROBE_BASE_URL", "https://grok.com").rstrip("/")
PROBE_PROXY = os.environ.get("SIGNER_PROBE_PROXY", "").strip()
HEALTHZ_PATH = os.environ.get("SIGNER_HEALTHZ_PROBE_PATH", "/rest/rate-limits")
UA = os.environ.get(
    "SIGNER_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
)

_PAIR_LOCK = threading.Lock()
_PAIR: SignerPair | None = None


def current_pair() -> SignerPair:
    global _PAIR
    with _PAIR_LOCK:
        if _PAIR is None:
            _PAIR = load_pair()
        return _PAIR


def reload_pair() -> SignerPair:
    global _PAIR
    with _PAIR_LOCK:
        _PAIR = load_pair()
        return _PAIR


def sign_request(method: str, path: str, pair: SignerPair | None = None) -> str:
    pair = pair or current_pair()
    method = method.upper().strip()
    if not method:
        raise ValueError("method required")
    if not path.startswith("/"):
        path = "/" + path
    return generate_statsig(method, path, pair.meta48, pair.fingerprint, trailer=pair.trailer)


def local_health_check() -> dict[str, Any]:
    pair = current_pair()
    sig = sign_request("POST", HEALTHZ_PATH, pair)
    if not valid_statsig_id(sig):
        raise RuntimeError("local /sign did not produce valid 70-byte statsig")
    return {"ok": True, "pair_loaded": True, "statsig_len": len(sig)}


def upstream_ready_check() -> dict[str, Any]:
    if not PROBE_PROXY:
        return {"ready": True, "probe": "skipped", "reason": "SIGNER_PROBE_PROXY unset"}
    sig = sign_request("POST", PROBE_PATH)
    url = PROBE_BASE_URL + PROBE_PATH
    body = json.dumps({"modelName": "fast"}).encode()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": PROBE_BASE_URL,
        "Referer": PROBE_BASE_URL + "/",
        "User-Agent": UA,
        "x-statsig-id": sig,
    }
    request = Request(url, data=body, headers=headers, method="POST")
    opener = build_opener(ProxyHandler({"http": PROBE_PROXY, "https": PROBE_PROXY}))
    try:
        with opener.open(request, timeout=20) as response:
            status = response.status
            text = response.read(512).decode("utf-8", "replace")
    except HTTPError as exc:
        status = exc.code
        text = exc.read(512).decode("utf-8", "replace")
    except URLError as exc:
        return {"ready": False, "probe": "failed", "error": type(exc).__name__}
    anti_bot = status == 403 and "anti-bot" in text.lower()
    ready = not anti_bot
    return {
        "ready": ready,
        "probe": "upstream",
        "http_status": status,
        "anti_bot": anti_bot,
    }


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class SignerHandler(BaseHTTPRequestHandler):
    server_version = "grok-signer/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            try:
                payload = local_health_check()
                _json_response(self, 200, payload)
            except Exception as exc:
                _json_response(self, 503, {"ok": False, "error": type(exc).__name__})
            return
        if path == "/readyz":
            try:
                local_health_check()
                payload = upstream_ready_check()
                status = 200 if payload.get("ready") else 503
                _json_response(self, status, payload)
            except Exception as exc:
                _json_response(self, 503, {"ready": False, "error": type(exc).__name__})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/sign":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 1 << 20:
            self.send_error(400)
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400)
            return
        method = str(payload.get("method") or "").strip()
        target_path = str(payload.get("path") or "").strip()
        if not method or not target_path:
            self.send_error(400)
            return
        # metaContent is accepted for protocol compatibility but signing uses the locked pair.
        try:
            sig = sign_request(method, target_path)
        except Exception as exc:
            LOG.warning("sign failed: %s", type(exc).__name__)
            _json_response(self, 500, {"error": "sign_failed"})
            return
        if not valid_statsig_id(sig):
            _json_response(self, 500, {"error": "invalid_signature"})
            return
        _json_response(self, 200, {"x-statsig-id": sig})


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(message)s")
    try:
        reload_pair()
    except PairLoadError as exc:
        LOG.error("pair load failed: %s", exc)
        return 1
    server = ThreadingHTTPServer(("0.0.0.0", PORT), SignerHandler)
    LOG.info("listening on :%s", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
