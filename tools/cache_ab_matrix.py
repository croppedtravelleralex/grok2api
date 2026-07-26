#!/usr/bin/env python3
"""Prompt-cache A/B matrix runner (cache-ab-v1).

Reads secrets from env or artifacts/cache-ab/.env (never commits secrets).
Writes runs.jsonl + summary.json under artifacts/cache-ab/{phase}/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT = REPO_ROOT / "artifacts" / "cache-ab"
SCHEMA_VERSION = "cache-ab-v1"
PREFIX_CHARS = 8000


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def git_meta() -> tuple[str, bool]:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = (
            subprocess.call(
                ["git", "diff", "--quiet"], cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            != 0
        )
        return sha, dirty
    except Exception:
        return "unknown", True


def build_prefix(seed: str) -> str:
    # Stable long prefix for cache threshold; content is deterministic per seed.
    unit = f"CACHE_AB_PREFIX:{seed}:abcdefghijklmnopqrstuvwxyz0123456789|"
    out = []
    while sum(len(x) for x in out) < PREFIX_CHARS:
        out.append(unit)
    text = "".join(out)
    return text[:PREFIX_CHARS]


HARD_FAIL_CLASSES = {"auth", "cf", "quota", "timeout", "network", "5xx", "credential"}


def classify_error(status: int, body: dict[str, Any] | None, exc: str | None) -> str:
    code = ""
    if body and isinstance(body.get("error"), dict):
        code = str((body.get("error") or {}).get("code") or "").lower()
    elif body and body.get("type") == "error" and isinstance(body.get("error"), dict):
        code = str((body.get("error") or {}).get("type") or "").lower()
    if exc:
        low = str(exc).lower()
        if "timed out" in low or "timeout" in low:
            return "timeout"
        return "network"
    if "credential" in code or "unavailable" in code:
        return "credential"
    if "network" in code or "transport" in code:
        return "network"
    if status in (401, 403) or "auth" in code or "unauthorized" in code:
        return "auth"
    if status == 429 or "quota" in code or "rate" in code:
        return "quota"
    if "cf" in code or "cloudflare" in code or "challenge" in code:
        return "cf"
    if status >= 500:
        return "5xx"
    if 400 <= status < 500:
        return "4xx"
    return ""

def http_json(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout: float,
) -> tuple[int, dict[str, Any] | None, str | None, float]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, method=method)
    for key, value in headers.items():
        req.add_header(key, value)
    started = time.perf_counter()
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            elapsed = time.perf_counter() - started
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return resp.status, {"_raw": raw[:2000]}, "parse", elapsed
            return resp.status, body, None, elapsed
    except HTTPError as err:
        elapsed = time.perf_counter() - started
        raw = err.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"_raw": raw[:2000]}
        return err.code, body, None, elapsed
    except URLError as err:
        elapsed = time.perf_counter() - started
        return 0, None, str(err.reason if hasattr(err, "reason") else err), elapsed
    except Exception as err:  # noqa: BLE001
        elapsed = time.perf_counter() - started
        return 0, None, str(err), elapsed


def reset_egress(db_path: str) -> None:
    if not db_path or not Path(db_path).is_file():
        return
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            """
            UPDATE egress_nodes
            SET cooldown_until = NULL, failure_count = 0, health = 1.0, last_error = ''
            WHERE scope = 'grok_build' OR id = 1
            """
        )
        con.commit()
    finally:
        con.close()


def fetch_audit(db_path: str, since_id: int) -> dict[str, Any] | None:
    if not db_path or not Path(db_path).is_file():
        return None
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            """
            SELECT id, request_id, event_id, operation, status_code, account_id, account_name,
                   provider, model_public_id, model_upstream_model, streaming,
                   usage_source, input_tokens, cached_input_tokens, output_tokens,
                   reasoning_tokens, total_tokens, duration_ms, error_code, cost_in_usd_ticks,
                   client_key_id, created_at
            FROM request_audits
            WHERE id > ?
            ORDER BY id DESC
            LIMIT 5
            """,
            (since_id,),
        ).fetchall()
        if not row:
            return None
        # Prefer newest matching recent window
        return dict(row[0])
    finally:
        con.close()


def max_audit_id(db_path: str) -> int:
    if not db_path or not Path(db_path).is_file():
        return 0
    con = sqlite3.connect(db_path)
    try:
        value = con.execute("SELECT COALESCE(MAX(id), 0) FROM request_audits").fetchone()[0]
        return int(value or 0)
    finally:
        con.close()


def extract_usage(operation: str, body: dict[str, Any] | None) -> dict[str, Any]:
    out = {
        "cache_read_input_tokens_response": None,
        "cached_tokens_upstream_raw": None,
        "response_input_tokens": None,
        "response_output_tokens": None,
    }
    if not body:
        return out
    usage = body.get("usage") or {}
    if operation == "messages":
        out["cache_read_input_tokens_response"] = usage.get("cache_read_input_tokens")
        out["response_input_tokens"] = usage.get("input_tokens")
        out["response_output_tokens"] = usage.get("output_tokens")
    else:
        details = usage.get("input_tokens_details") or {}
        out["cached_tokens_upstream_raw"] = details.get("cached_tokens")
        out["response_input_tokens"] = usage.get("input_tokens") or usage.get("prompt_tokens")
        out["response_output_tokens"] = usage.get("output_tokens") or usage.get("completion_tokens")
    return out


def case_specs(model: str, prefix: str, stable_key: str) -> list[dict[str, Any]]:
    """Return ordered cases; each has round builders."""

    def messages_body(system_text: str, user_text: str, with_cache: bool) -> dict[str, Any]:
        system: Any
        if with_cache:
            system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
        else:
            system = system_text
        return {
            "model": model,
            "max_tokens": 32,
            "stream": False,
            "system": system,
            "messages": [{"role": "user", "content": user_text}],
        }

    def responses_body(text: str, cache_key: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "input": text,
            "stream": False,
            "max_output_tokens": 32,
        }
        if cache_key is not None:
            body["prompt_cache_key"] = cache_key
        return body

    def chat_body(system_text: str, user_text: str) -> dict[str, Any]:
        return {
            "model": model,
            "stream": False,
            "max_tokens": 32,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
        }

    prefix_b = build_prefix(sha256_text(prefix)[:8] + "-alt")

    return [
        {
            "case_id": "M-R",
            "arm": "R",
            "operation": "messages",
            "endpoint": "/v1/messages",
            "sticky_expected": False,
            "has_cache_control": False,
            "rounds": [
                {"round": 1, "payload": messages_body(prefix, "tail-round-1-alpha", False)},
                {"round": 2, "payload": messages_body(prefix, "tail-round-2-beta", False)},
            ],
        },
        {
            "case_id": "M-S",
            "arm": "S",
            "operation": "messages",
            "endpoint": "/v1/messages",
            "sticky_expected": True,
            "has_cache_control": True,
            "rounds": [
                {"round": 1, "payload": messages_body(prefix, "tail-round-1-alpha", True)},
                {"round": 2, "payload": messages_body(prefix, "tail-round-2-beta", True)},
            ],
        },
        {
            "case_id": "M-S-miss",
            "arm": "S-miss",
            "operation": "messages",
            "endpoint": "/v1/messages",
            "sticky_expected": True,
            "has_cache_control": True,
            "rounds": [
                {"round": 1, "payload": messages_body(prefix, "tail-round-1-alpha", True)},
                {"round": 2, "payload": messages_body(prefix_b, "tail-round-2-beta", True)},
            ],
        },
        {
            "case_id": "R-R",
            "arm": "R",
            "operation": "responses",
            "endpoint": "/v1/responses",
            "sticky_expected": False,
            "has_cache_control": False,
            "rounds": [
                {"round": 1, "payload": responses_body(prefix + "\n\nUSER: tail-round-1-alpha", None)},
                {"round": 2, "payload": responses_body(prefix + "\n\nUSER: tail-round-2-beta", None)},
            ],
        },
        {
            "case_id": "R-S",
            "arm": "S",
            "operation": "responses",
            "endpoint": "/v1/responses",
            "sticky_expected": True,
            "has_cache_control": False,
            "client_key": stable_key,
            "rounds": [
                {
                    "round": 1,
                    "payload": responses_body(prefix + "\n\nUSER: tail-round-1-alpha", stable_key),
                },
                {
                    "round": 2,
                    "payload": responses_body(prefix + "\n\nUSER: tail-round-2-beta", stable_key),
                },
            ],
        },
        {
            "case_id": "R-S-diffkey",
            "arm": "S-diffkey",
            "operation": "responses",
            "endpoint": "/v1/responses",
            "sticky_expected": False,
            "has_cache_control": False,
            "rounds": [
                {
                    "round": 1,
                    "payload": responses_body(prefix + "\n\nUSER: tail-round-1-alpha", stable_key + "-a"),
                },
                {
                    "round": 2,
                    "payload": responses_body(prefix + "\n\nUSER: tail-round-2-beta", stable_key + "-b"),
                },
            ],
        },
        {
            "case_id": "C-ctrl",
            "arm": "ctrl",
            "operation": "chat",
            "endpoint": "/v1/chat/completions",
            "sticky_expected": False,
            "has_cache_control": False,
            "rounds": [
                {"round": 1, "payload": chat_body(prefix, "tail-round-1-alpha")},
                {"round": 2, "payload": chat_body(prefix, "tail-round-2-beta")},
            ],
        },
    ]


def headers_for(operation: str, api_key: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if operation == "messages":
        headers["anthropic-version"] = "2023-06-01"
    return headers


def prompt_meta(payload: dict[str, Any], operation: str) -> dict[str, Any]:
    prefix = ""
    tail = ""
    client_key = payload.get("prompt_cache_key")
    if operation == "messages":
        system = payload.get("system")
        if isinstance(system, str):
            prefix = system
        elif isinstance(system, list) and system:
            first = system[0]
            if isinstance(first, dict):
                prefix = str(first.get("text") or "")
        msgs = payload.get("messages") or []
        if msgs:
            content = msgs[-1].get("content")
            tail = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    elif operation == "responses":
        text = str(payload.get("input") or "")
        if "\n\nUSER:" in text:
            prefix, tail = text.split("\n\nUSER:", 1)
        else:
            prefix, tail = text, ""
    else:
        msgs = payload.get("messages") or []
        for msg in msgs:
            if msg.get("role") == "system":
                prefix = str(msg.get("content") or "")
            if msg.get("role") == "user":
                tail = str(msg.get("content") or "")
    return {
        "prompt_prefix_chars": len(prefix),
        "prompt_prefix_sha256": sha256_text(prefix) if prefix else "",
        "prompt_tail_sha256": sha256_text(tail) if tail else "",
        "key_fingerprint": sha256_text(str(client_key)) if client_key else "",
        "prompt_cache_key_client": bool(client_key),
    }


def _pair_hard_fail(pair: dict[str, Any]) -> bool:
    for key in ("r1", "r2"):
        row = pair.get(key) or {}
        if row.get("error_class") in HARD_FAIL_CLASSES:
            return True
        if int(row.get("http_status") or 0) != 200:
            return True
    return False


def evaluate_gate(case_stats: dict[str, Any]) -> dict[str, Any]:
    def majority_hit(case_id: str) -> tuple[int, int, int]:
        stats = case_stats.get(case_id) or {}
        pairs = stats.get("pairs") or []
        hits = 0
        sticky = 0
        hard_fail = 0
        for pair in pairs:
            if _pair_hard_fail(pair):
                hard_fail += 1
                continue
            r2 = pair.get("r2") or {}
            if int(r2.get("cached_input_tokens") or 0) > 0:
                hits += 1
            if r2.get("same_account_as_round1"):
                sticky += 1
        valid = len(pairs) - hard_fail
        return hits, sticky, valid

    def majority_miss(case_id: str) -> tuple[int, int]:
        stats = case_stats.get(case_id) or {}
        pairs = stats.get("pairs") or []
        misses = 0
        hard_fail = 0
        for pair in pairs:
            if _pair_hard_fail(pair):
                hard_fail += 1
                continue
            r2 = pair.get("r2") or {}
            if int(r2.get("cached_input_tokens") or 0) == 0:
                misses += 1
        valid = len(pairs) - hard_fail
        return misses, valid

    ms_hits, ms_sticky, ms_valid = majority_hit("M-S")
    rs_hits, rs_sticky, rs_valid = majority_hit("R-S")
    mr_miss, mr_valid = majority_miss("M-R")
    rr_miss, rr_valid = majority_miss("R-R")
    miss_hits, _, miss_valid = majority_hit("M-S-miss")
    diff_hits, _, diff_valid = majority_hit("R-S-diffkey")

    inconclusive = False
    for case_id in ("M-S", "R-S", "M-R", "R-R"):
        stats = case_stats.get(case_id) or {}
        pairs = stats.get("pairs") or []
        hard = sum(1 for p in pairs if _pair_hard_fail(p))
        # Need at least 2 valid pairs; otherwise inconclusive.
        if len(pairs) - hard < 2:
            inconclusive = True

    positive_ok = (ms_valid >= 2 and ms_hits >= 2 and ms_sticky >= 2) or (
        rs_valid >= 2 and rs_hits >= 2 and rs_sticky >= 2
    )
    negative_ok = (mr_valid >= 2 and mr_miss >= 2) and (rr_valid >= 2 and rr_miss >= 2)
    false_hit_ok = True
    if miss_valid >= 2 and miss_hits >= 2:
        false_hit_ok = False
    if diff_valid >= 2 and diff_hits >= 2:
        false_hit_ok = False

    both_hit = (
        ((ms_valid >= 2 and ms_hits >= 2) or (rs_valid >= 2 and rs_hits >= 2))
        and ((mr_valid >= 2 and (mr_valid - mr_miss) >= 2) or (rr_valid >= 2 and (rr_valid - rr_miss) >= 2))
    )

    if inconclusive:
        decision = "INCONCLUSIVE"
    elif positive_ok and negative_ok and false_hit_ok:
        decision = "ACCEPT"
    elif both_hit:
        decision = "REJECT-ALT"
    elif not positive_ok:
        decision = "REJECT"
    else:
        decision = "REJECT"
    return {
        "decision": decision,
        "positive_ok": positive_ok,
        "negative_ok": negative_ok,
        "false_hit_ok": false_hit_ok,
        "metrics": {
            "M-S": {"hits": ms_hits, "sticky": ms_sticky, "valid": ms_valid},
            "R-S": {"hits": rs_hits, "sticky": rs_sticky, "valid": rs_valid},
            "M-R": {"misses": mr_miss, "valid": mr_valid},
            "R-R": {"misses": rr_miss, "valid": rr_valid},
            "M-S-miss": {"hits": miss_hits, "valid": miss_valid},
            "R-S-diffkey": {"hits": diff_hits, "valid": diff_valid},
        },
        "hypothesis_support": decision == "ACCEPT",
    }


def run_matrix(args: argparse.Namespace) -> int:
    load_dotenv(DEFAULT_ARTIFACT / ".env")
    base = os.environ.get("G2A_BASE_URL", args.base_url).rstrip("/")
    api_key = os.environ.get("G2A_API_KEY", args.api_key)
    model = os.environ.get("G2A_MODEL", args.model)
    db_path = os.environ.get("G2A_DB", args.db)
    if not api_key:
        print("G2A_API_KEY missing", file=sys.stderr)
        return 2

    phase = args.phase
    repeats = args.repeats
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_ARTIFACT / phase
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    run_id = str(uuid.uuid4())
    git_sha, git_dirty = git_meta()
    prefix = build_prefix(f"{phase}-{model}")
    stable_key = f"cache-ab-{phase}-{sha256_text(run_id)[:12]}"
    cases = case_specs(model, prefix, stable_key)

    # env readiness
    status, body, err, _ = http_json("GET", f"{base}/healthz", {}, None, 10)
    if status != 200:
        print(f"healthz failed status={status} err={err} body={body}", file=sys.stderr)
        return 3

    runs_path = out_dir / "runs.jsonl"
    records: list[dict[str, Any]] = []
    case_stats: dict[str, Any] = {c["case_id"]: {"pairs": []} for c in cases}

    with runs_path.open("w", encoding="utf-8") as fh:
        for repeat_index in range(repeats):
            for case in cases:
                pair: dict[str, Any] = {}
                account_r1 = None
                for round_spec in case["rounds"]:
                    if args.reset_egress:
                        reset_egress(db_path)
                    before_id = max_audit_id(db_path)
                    payload = round_spec["payload"]
                    meta = prompt_meta(payload, case["operation"])
                    started_at = utc_now()
                    t0 = time.perf_counter()
                    status_code, resp_body, transport_err, http_elapsed = http_json(
                        "POST",
                        f"{base}{case['endpoint']}",
                        headers_for(case["operation"], api_key),
                        payload,
                        args.timeout,
                    )
                    ended_at = utc_now()
                    audit = fetch_audit(db_path, before_id)
                    usage_bits = extract_usage(case["operation"], resp_body)
                    error_class = classify_error(status_code, resp_body, transport_err)
                    cached = None
                    if audit is not None:
                        cached = audit.get("cached_input_tokens")
                    if cached is None:
                        cached = usage_bits.get("cached_tokens_upstream_raw")
                        if cached is None:
                            cached = usage_bits.get("cache_read_input_tokens_response")
                    cached_i = int(cached or 0)
                    account_id = (audit or {}).get("account_id")
                    same_account = None
                    if round_spec["round"] == 1:
                        account_r1 = account_id
                    else:
                        same_account = (
                            account_r1 is not None
                            and account_id is not None
                            and str(account_r1) == str(account_id)
                        )

                    # redact response dump
                    raw_name = f"{case['case_id']}_r{repeat_index}_round{round_spec['round']}.json"
                    if resp_body is not None:
                        dump = dict(resp_body)
                        if "output" in dump:
                            dump["output"] = "[redacted]"
                        if "choices" in dump:
                            dump["choices"] = "[redacted]"
                        if "content" in dump:
                            dump["content"] = "[redacted]"
                        (raw_dir / raw_name).write_text(
                            json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8"
                        )

                    record = {
                        "schema_version": SCHEMA_VERSION,
                        "phase": phase,
                        "run_id": run_id,
                        "case_id": case["case_id"],
                        "arm": case["arm"],
                        "round": round_spec["round"],
                        "repeat_index": repeat_index,
                        "started_at": started_at,
                        "ended_at": ended_at,
                        "git_sha": git_sha,
                        "git_dirty": git_dirty,
                        "listen_addr": base.replace("http://", "").replace("https://", ""),
                        "env": "windows-local",
                        "operation": case["operation"],
                        "endpoint": case["endpoint"],
                        "model_public": model,
                        "streaming": False,
                        "has_cache_control": case["has_cache_control"],
                        "prompt_cache_key_client": meta["prompt_cache_key_client"],
                        "key_fingerprint": meta["key_fingerprint"],
                        "prompt_prefix_chars": meta["prompt_prefix_chars"],
                        "prompt_prefix_sha256": meta["prompt_prefix_sha256"],
                        "prompt_tail_sha256": meta["prompt_tail_sha256"],
                        "http_status": status_code,
                        "request_id": (audit or {}).get("request_id"),
                        "event_id": (audit or {}).get("event_id"),
                        "account_id": account_id,
                        "account_name": (audit or {}).get("account_name"),
                        "provider": (audit or {}).get("provider"),
                        "model_upstream": (audit or {}).get("model_upstream_model"),
                        "sticky_expected": case["sticky_expected"],
                        "same_account_as_round1": same_account,
                        "client_key_id": (audit or {}).get("client_key_id"),
                        "usage_source": (audit or {}).get("usage_source"),
                        "input_tokens": (audit or {}).get("input_tokens")
                        or usage_bits.get("response_input_tokens"),
                        "cached_input_tokens": cached_i,
                        "cache_read_input_tokens_response": usage_bits.get(
                            "cache_read_input_tokens_response"
                        ),
                        "cached_tokens_upstream_raw": usage_bits.get("cached_tokens_upstream_raw"),
                        "output_tokens": (audit or {}).get("output_tokens")
                        or usage_bits.get("response_output_tokens"),
                        "reasoning_tokens": (audit or {}).get("reasoning_tokens"),
                        "total_tokens": (audit or {}).get("total_tokens"),
                        "duration_ms": (audit or {}).get("duration_ms")
                        or int(http_elapsed * 1000),
                        "error_code": (audit or {}).get("error_code")
                        or (
                            ((resp_body or {}).get("error") or {}).get("code")
                            if isinstance(resp_body, dict)
                            else None
                        ),
                        "error_class": error_class,
                        "cost_in_usd_ticks": (audit or {}).get("cost_in_usd_ticks"),
                        "cache_hit": cached_i > 0,
                        "wall_ms": int((time.perf_counter() - t0) * 1000),
                        "raw_file": raw_name,
                    }
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                    fh.flush()
                    records.append(record)
                    pair[f"r{round_spec['round']}"] = record
                    print(
                        f"[{phase}] {case['case_id']} rep={repeat_index} round={round_spec['round']} "
                        f"status={status_code} cached={cached_i} account={account_id} err={error_class or '-'}",
                        flush=True,
                    )
                    # Pace requests to protect egress health / OAuth refresh.
                    time.sleep(args.pace)
                case_stats[case["case_id"]]["pairs"].append(pair)
                time.sleep(args.pace)

    gate = evaluate_gate(case_stats)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "phase": phase,
        "run_id": run_id,
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "model": model,
        "base_url": base,
        "prefix_chars": PREFIX_CHARS,
        "repeats": repeats,
        "record_count": len(records),
        "case_stats": {
            cid: {
                "pair_count": len(stats["pairs"]),
                "round2_hits": sum(
                    1 for p in stats["pairs"] if int((p.get("r2") or {}).get("cached_input_tokens") or 0) > 0
                ),
                "sticky_ok": sum(1 for p in stats["pairs"] if (p.get("r2") or {}).get("same_account_as_round1")),
            }
            for cid, stats in case_stats.items()
        },
        "gate": gate,
        "completed_at": utc_now(),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"gate": gate, "out_dir": str(out_dir)}, ensure_ascii=False, indent=2))
    return 0 if gate["decision"] != "INCONCLUSIVE" else 4


def compare_phases(baseline_dir: Path, after_dir: Path, out_path: Path) -> int:
    base = json.loads((baseline_dir / "summary.json").read_text(encoding="utf-8"))
    after = json.loads((after_dir / "summary.json").read_text(encoding="utf-8"))
    lines = [
        "# cache-ab compare",
        "",
        f"- baseline decision: `{base.get('gate', {}).get('decision')}`",
        f"- after decision: `{after.get('gate', {}).get('decision')}`",
        "",
        "| case | baseline r2 hits | after r2 hits | baseline sticky | after sticky |",
        "|------|------------------|---------------|-----------------|--------------|",
    ]
    for case_id in sorted(set(base.get("case_stats", {})) | set(after.get("case_stats", {}))):
        b = base.get("case_stats", {}).get(case_id, {})
        a = after.get("case_stats", {}).get(case_id, {})
        lines.append(
            f"| {case_id} | {b.get('round2_hits')} | {a.get('round2_hits')} | {b.get('sticky_ok')} | {a.get('sticky_ok')} |"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Prompt cache A/B matrix")
    parser.add_argument("--phase", choices=["baseline", "after"], default="baseline")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--model", default="Build/grok-4.5")
    parser.add_argument("--db", default=str(REPO_ROOT / "data" / "backend.db"))
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--pace", type=float, default=2.0, help="seconds between requests")
    parser.add_argument(
        "--reset-egress",
        action="store_true",
        help="clear local Build egress cooldown between requests (local test only)",
    )
    parser.add_argument("--compare", action="store_true")
    args = parser.parse_args()
    if args.compare:
        return compare_phases(
            DEFAULT_ARTIFACT / "baseline",
            DEFAULT_ARTIFACT / "after",
            DEFAULT_ARTIFACT / "compare.md",
        )
    return run_matrix(args)


if __name__ == "__main__":
    raise SystemExit(main())
