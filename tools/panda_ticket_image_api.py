#!/usr/bin/env python3
"""Experimental HTTP API: pop ticket from pool → Lite image → return JPEG.

Run on Panda:
  python3 /opt/grok2api/tools/panda_ticket_image_api.py --port 18789

Not integrated into grok2api gateway yet.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOOLS = Path("/opt/grok2api/tools")
POOL_SCRIPT = TOOLS / "panda_ticket_pool.py"
LITE_SCRIPT = TOOLS / "panda_lite_with_ticket.py"


def load_pool_mod():
    spec = importlib.util.spec_from_file_location("pool", POOL_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def load_lite_mod():
    spec = importlib.util.spec_from_file_location("lite", LITE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


class Handler(BaseHTTPRequestHandler):
    pool = None
    lite = None

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/healthz":
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.rstrip("/") == "/v1/pool/stats":
            conn = self.pool.connect(self.pool.DEFAULT_DB)
            body = json.dumps(self.pool.stats(conn), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/ticket-image":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400, "invalid json")
            return
        prompt = str(req.get("prompt") or "Drawing: a red apple on white table, product photo")
        account_id = req.get("account_id")
        account_id = int(account_id) if account_id is not None else None

        conn = self.pool.connect(self.pool.DEFAULT_DB)
        row = self.pool.pop(conn, account_id=account_id)
        if not row:
            body = json.dumps({"error": "no_ticket_available"}, ensure_ascii=False).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        ticket = self.pool.ticket_to_lite_payload(row, prompt)
        proc = subprocess.run(
            [sys.executable, str(LITE_SCRIPT)],
            input=json.dumps(ticket, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            timeout=300,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")[-500:]
            body = json.dumps({"error": "lite_failed", "detail": err}, ensure_ascii=False).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        out_dir = Path(self.lite.OUT)
        summary_path = out_dir / "summary.json"
        if not summary_path.exists():
            self.send_error(500, "no summary")
            return
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        saved = summary.get("saved_file") or ""
        if not saved or not Path(saved).is_file():
            body = json.dumps({"error": "no_image", "summary": summary}, ensure_ascii=False).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        img = Path(saved).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("X-Pool-Id", row["id"])
        self.send_header("X-Account-Id", str(row["account_id"]))
        self.send_header("Content-Length", str(len(img)))
        self.end_headers()
        self.wfile.write(img)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18789)
    args = parser.parse_args()

    Handler.pool = load_pool_mod()
    Handler.lite = load_lite_mod()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(json.dumps({"event": "listen", "host": args.host, "port": args.port}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
