#!/usr/bin/env python3
"""抽样测试 Webshare 代理对 grok.com / assets.grok.com 的可达性。"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def parse_proxy(line: str) -> str:
    parts = line.strip().split(":")
    if len(parts) < 4:
        raise ValueError(f"bad line: {line[:60]}")
    host, port, user = parts[0], parts[1], parts[2]
    password = ":".join(parts[3:])
    return f"http://{user}:{password}@{host}:{port}"


def probe(proxy_url: str, target: str, timeout: float) -> dict:
    # 通过 curl_cffi 不可用时代理直连探针：仅测 TCP+HTTP 状态（无 TLS 指纹）
    import subprocess

    cmd = [
        "curl",
        "-sS",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "--max-time",
        str(int(timeout)),
        "-x",
        proxy_url,
        target,
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
        code = int(out[-3:]) if out else 0
        return {"ok": code in (200, 301, 302, 403), "status": code, "error": ""}
    except subprocess.CalledProcessError as exc:
        return {"ok": False, "status": 0, "error": exc.output[-200:]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--sample", type=int, default=10, help="max proxies to test")
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument(
        "--targets",
        default="https://grok.com/,https://assets.grok.com/",
        help="comma-separated URLs",
    )
    args = parser.parse_args()
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    lines = [ln.strip() for ln in Path(args.file).read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    lines = lines[: args.sample]
    results = []
    for index, line in enumerate(lines, start=1):
        proxy = parse_proxy(line)
        row = {"index": index, "proxy_host": line.split(":")[0]}
        for target in targets:
            row[target] = probe(proxy, target, args.timeout)
        results.append(row)
    summary = {}
    for target in targets:
        ok = sum(1 for r in results if r[target]["ok"])
        summary[target] = {"tested": len(results), "ok": ok}
    print(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
