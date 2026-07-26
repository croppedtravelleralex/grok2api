#!/usr/bin/env python3
"""FROZEN baseline pointer — do not evolve logic here.

Canonical doc:    docs/http-reverse-lite-chain.md
Canonical entry:  tools/web_chrome_http_lite_frozen.py
Implementation:   tools/_chrome_channel_chat_image.py
Shared HTTP/helpers: tools/web_http_chat_image_canary.v1.py
Bench:            tools/_chrome_lite_bench.py

Legacy bundled-Chromium canary (superseded): tools/web_http_chat_image_canary.v1.py __main__
"""
from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("web_chrome_http_lite_frozen.py")), run_name="__main__")
