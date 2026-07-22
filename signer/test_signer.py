#!/usr/bin/env python3
"""Unit tests for grok-signer sidecar."""
from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app as signer_app
from pair import PairLoadError, load_pair
from statsig import generate_statsig, valid_statsig_id

ROOT = Path(__file__).resolve().parent
META48 = bytes(range(48))
FP = "deadbeefcafebabe"
PAIR_JSON = {
    "meta_b64": base64.b64encode(META48).decode(),
    "fingerprint": FP,
    "trailer_hex": "03",
}


class StatsigTest(unittest.TestCase):
    def test_generate_statsig_produces_70_bytes(self) -> None:
        sig = generate_statsig("POST", "/rest/rate-limits", META48, FP, n=12345, key=7)
        self.assertTrue(valid_statsig_id(sig))

    def test_generate_statsig_rejects_bad_meta(self) -> None:
        with self.assertRaises(ValueError):
            generate_statsig("POST", "/x", b"short", FP)


class PairLoadTest(unittest.TestCase):
    def test_load_from_pair_file(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            json.dump(PAIR_JSON, tmp)
            path = tmp.name
        self.addCleanup(lambda: os.unlink(path))
        os.environ["SIGNER_PAIR_FILE"] = path
        os.environ.pop("SIGNER_SEED_HEX", None)
        os.environ.pop("SIGNER_HEX", None)
        pair = load_pair()
        self.assertEqual(pair.meta48, META48)
        self.assertEqual(pair.fingerprint, FP)

    def test_load_from_seed_hex_env(self) -> None:
        os.environ.pop("SIGNER_PAIR_FILE", None)
        os.environ["SIGNER_SEED_HEX"] = META48.hex()
        os.environ["SIGNER_HEX"] = FP
        pair = load_pair()
        self.assertEqual(pair.meta48, META48)
        self.assertEqual(pair.fingerprint, FP)

    def test_missing_pair_raises(self) -> None:
        os.environ.pop("SIGNER_PAIR_FILE", None)
        os.environ.pop("SIGNER_SEED_HEX", None)
        os.environ.pop("SIGNER_HEX", None)
        with self.assertRaises(PairLoadError):
            load_pair()


class HttpServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        json.dump(PAIR_JSON, cls._tmp)
        cls._tmp.close()
        os.environ["SIGNER_PAIR_FILE"] = cls._tmp.name
        os.environ.pop("SIGNER_PROBE_PROXY", None)
        signer_app.reload_pair()
        cls._server = ThreadingHTTPServer(("127.0.0.1", 0), signer_app.SignerHandler)
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()
        cls.base = f"http://127.0.0.1:{cls._server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server.shutdown()
        cls._server.server_close()
        os.unlink(cls._tmp.name)

    def test_healthz_ok(self) -> None:
        with urlopen(self.base + "/healthz", timeout=3) as resp:
            payload = json.loads(resp.read().decode())
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["pair_loaded"])

    def test_readyz_skips_without_proxy(self) -> None:
        with urlopen(self.base + "/readyz", timeout=3) as resp:
            payload = json.loads(resp.read().decode())
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["probe"], "skipped")

    def test_sign_matches_backend_protocol(self) -> None:
        body = json.dumps(
            {
                "method": "POST",
                "path": "/rest/app-chat/conversations/new",
                "environment": {"metaContent": "ignored-html-meta"},
            }
        ).encode()
        req = Request(self.base + "/sign", data=body, method="POST", headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=3) as resp:
            payload = json.loads(resp.read().decode())
        self.assertTrue(valid_statsig_id(payload["x-statsig-id"]))

    def test_sign_rejects_invalid_payload(self) -> None:
        req = Request(self.base + "/sign", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=3)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
