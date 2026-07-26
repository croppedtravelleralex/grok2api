#!/usr/bin/env python3
"""Run one Chrome ticket lifecycle batch (<=10min each)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from chrome_ticket_experiment_lib import (
    DEFAULT_LOG,
    DEFAULT_PROMPT,
    DEFAULT_SSO,
    append_experiment_log,
    ensure_pool_depth,
    log_event,
    mint_to_pool,
    panda_image_once,
    parse_duration,
    pool_available,
    pool_stats,
    push_ticket,
    sleep_countdown,
    verdict,
)


def cmd_stats(_: argparse.Namespace) -> int:
    stats = pool_stats()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def cmd_s0(args: argparse.Namespace) -> int:
    account_id = args.account_id
    sso = Path(args.sso_file)
    if args.mint or pool_available(account_id) < 1:
        log_event("s0_mint", account_id=account_id)
        mint_to_pool(account_id, sso, timeout=args.mint_timeout)
    result = panda_image_once(prompt=args.prompt)
    row = append_experiment_log(
        "S0",
        log_path=Path(args.log),
        account_id=account_id,
        hypothesis="baseline chain 200 with pool_hit",
        http=result["http"],
        wall_ms=result["wall_ms"],
        pool_hit=result["pool_hit"],
        asset403=result.get("asset403", 0),
        cf_warm=result.get("cf_warm", False),
        url=result.get("url", ""),
        result=verdict(result["http"], result["pool_hit"]),
    )
    print(json.dumps(row, ensure_ascii=False))
    return 0 if row["result"] == "pass" else 1


def cmd_delay(args: argparse.Namespace) -> int:
    wait_s = parse_duration(args.wait)
    batch = args.batch or {0: "D0", 300: "D1", 1800: "D2"}.get(wait_s, f"D-{wait_s}s")
    account_id = args.account_id
    sso = Path(args.sso_file)
    minted = mint_to_pool(account_id, sso, timeout=args.mint_timeout, ttl_hours=args.ttl_hours)
    mint_ts = minted["minted_at"]
    sleep_countdown(wait_s, label="delay")
    result = panda_image_once(prompt=args.prompt)
    row = append_experiment_log(
        batch,
        log_path=Path(args.log),
        account_id=account_id,
        hypothesis=f"delayed consume after {wait_s}s",
        ticket_id=minted["pushed"].get("id"),
        minted_at=mint_ts,
        wait_s=wait_s,
        ticket_age_s=wait_s,
        http=result["http"],
        wall_ms=result["wall_ms"],
        pool_hit=result["pool_hit"],
        result=verdict(result["http"], result["pool_hit"]),
    )
    print(json.dumps(row, ensure_ascii=False))
    return 0 if row["result"] == "pass" else 1


def cmd_survival(args: argparse.Namespace) -> int:
    age_s = parse_duration(args.age)
    batch = args.batch or f"S-{args.age}"
    account_id = args.account_id
    sso = Path(args.sso_file)
    if args.skip_mint and pool_available(account_id) > 0:
        log_event("survival_skip_mint", available=pool_available(account_id))
        minted = {"pushed": {"id": "existing"}, "minted_at": None}
        sleep_countdown(age_s, label="survival")
    else:
        minted = mint_to_pool(account_id, sso, timeout=args.mint_timeout, ttl_hours=args.ttl_hours)
        sleep_countdown(age_s, label="survival")
    result = panda_image_once(prompt=args.prompt)
    row = append_experiment_log(
        batch,
        log_path=Path(args.log),
        account_id=account_id,
        hypothesis=f"meta valid at age {age_s}s",
        ticket_id=minted["pushed"].get("id"),
        minted_at=minted.get("minted_at"),
        ticket_age_s=age_s,
        ttl_hours=args.ttl_hours,
        http=result["http"],
        wall_ms=result["wall_ms"],
        pool_hit=result["pool_hit"],
        result=verdict(result["http"], result["pool_hit"]),
    )
    print(json.dumps(row, ensure_ascii=False))
    return 0 if row["result"] == "pass" else 1


def cmd_reuse(args: argparse.Namespace) -> int:
    variant = args.variant.lower()
    account_id = args.account_id
    sso = Path(args.sso_file)
    if variant == "r1":
        minted = mint_to_pool(account_id, sso, timeout=args.mint_timeout)
        result = panda_image_once(prompt=args.prompt)
        row = append_experiment_log(
            "R1",
            log_path=Path(args.log),
            account_id=account_id,
            variant="r1",
            ticket_id=minted["pushed"].get("id"),
            http=result["http"],
            pool_hit=result["pool_hit"],
            result=verdict(result["http"], result["pool_hit"]),
        )
        print(json.dumps(row, ensure_ascii=False))
        return 0 if row["result"] == "pass" else 1

    if variant == "r2":
        minted = mint_to_pool(account_id, sso, timeout=args.mint_timeout)
        first = panda_image_once(prompt=args.prompt)
        ticket_row = minted["row"]
        dump = Path(args.dump or (Path(args.log).parent / f"ticket-reuse-{account_id}.json"))
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(json.dumps(ticket_row, ensure_ascii=False, indent=2), encoding="utf-8")
        repush = push_ticket(ticket_row, ttl_hours=args.ttl_hours)
        second = panda_image_once(prompt=args.prompt)
        row = append_experiment_log(
            "R2",
            log_path=Path(args.log),
            account_id=account_id,
            variant="r2",
            first_http=first["http"],
            first_pool_hit=first["pool_hit"],
            second_http=second["http"],
            second_pool_hit=second["pool_hit"],
            repush_ticket_id=repush.get("id"),
            dump=str(dump),
            result=verdict(second["http"], second["pool_hit"]),
            notes="second consume tests meta re-push not pool record reuse",
        )
        print(json.dumps(row, ensure_ascii=False))
        return 0 if row["result"] == "pass" else 1

    raise SystemExit(f"unknown reuse variant {variant!r}, use r1 or r2")


def cmd_cross_session(args: argparse.Namespace) -> int:
    account_id = args.account_id
    sso = Path(args.sso_file)
    batch = "C1" if args.round == 1 else "C2"
    minted = mint_to_pool(account_id, sso, timeout=args.mint_timeout)
    result = panda_image_once(prompt=args.prompt)
    row = append_experiment_log(
        batch,
        log_path=Path(args.log),
        account_id=account_id,
        round=args.round,
        hypothesis="fresh playwright subprocess per mint",
        ticket_id=minted["pushed"].get("id"),
        http=result["http"],
        pool_hit=result["pool_hit"],
        result=verdict(result["http"], result["pool_hit"]),
    )
    print(json.dumps(row, ensure_ascii=False))
    return 0 if row["result"] == "pass" else 1


def cmd_concurrent(args: argparse.Namespace) -> int:
    account_id = args.account_id
    sso = Path(args.sso_file)
    n = args.concurrency
    batch = f"M{n}"
    needed = n if args.require_tickets else min(n, max(1, pool_available(account_id)))
    if args.mint:
        ensure_pool_depth(account_id, sso, needed, timeout=args.mint_timeout)
    available_before = pool_available(account_id)
    script = f"""
import concurrent.futures, json, subprocess, time, urllib.error, urllib.request
KEY=open("/root/.secrets/grok2api-newapi-key").read().strip()
BASE="http://127.0.0.1:18000"
PROMPT={json.dumps(args.prompt)}
N={n}

def one(i):
    started=time.time()
    body=json.dumps({{"model":"grok-imagine-image","prompt":PROMPT,"size":"1024x1024","n":1}}).encode()
    req=urllib.request.Request(BASE+"/v1/images/generations", data=body, method="POST", headers={{
        "Authorization":"Bearer "+KEY, "Content-Type":"application/json"}})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw=resp.read(); http=resp.status
    except urllib.error.HTTPError as exc:
        http=exc.code
    return {{"idx":i,"http":http,"wall_ms":int((time.time()-started)*1000)}}

with concurrent.futures.ThreadPoolExecutor(max_workers=N) as ex:
    rows=list(ex.map(one, range(N)))
logs=subprocess.run(["docker","logs","grok2api","--since","240s"], capture_output=True, text=True)
pool_hits=logs.stdout.count("chrome_ticket_pool_hit")
print(json.dumps({{"results":rows,"pool_hits":pool_hits}}))
"""
    from chrome_ticket_experiment_lib import run_remote_python

    out = run_remote_python(script, remote_name="_chrome_ticket_concurrent.py", timeout=max(300, n * 200))
    payload = json.loads(out.splitlines()[-1])
    results = payload["results"]
    ok = sum(1 for r in results if r["http"] == 200)
    row = append_experiment_log(
        batch,
        log_path=Path(args.log),
        account_id=account_id,
        concurrency=n,
        available_before=available_before,
        pool_hits=payload.get("pool_hits", 0),
        http200=ok,
        results=results,
        result="pass" if ok >= min(n, available_before) else "inconclusive",
        notes="pool_hits may be < concurrency when pool shallow",
    )
    print(json.dumps(row, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Chrome ticket batch experiments")
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--account-id", type=int, default=1467)
    parser.add_argument("--sso-file", default=str(DEFAULT_SSO))
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--mint-timeout", type=int, default=120)
    parser.add_argument("--ttl-hours", type=float, default=12.0)
    sub = parser.add_subparsers(dest="cmd", required=True)

    stats_p = sub.add_parser("stats", help="print pool stats")
    stats_p.set_defaults(func=cmd_stats)

    s0_p = sub.add_parser("s0", help="baseline: ensure ticket + single image")
    s0_p.add_argument("--mint", action="store_true", help="always mint before consume")
    s0_p.set_defaults(func=cmd_s0)

    delay_p = sub.add_parser("delay", help="Batch D: mint, wait, consume")
    delay_p.add_argument("--wait", default="0", help="0, 5m, 30m, …")
    delay_p.add_argument("--batch", help="override batch id (D0/D1/D2)")
    delay_p.set_defaults(func=cmd_delay)

    survival_p = sub.add_parser("survival", help="Batch S: mint, age, consume")
    survival_p.add_argument("--age", default="0", help="ticket age before consume")
    survival_p.add_argument("--batch", help="override batch id")
    survival_p.add_argument("--skip-mint", action="store_true", help="use existing pool ticket")
    survival_p.set_defaults(func=cmd_survival)

    reuse_p = sub.add_parser("reuse", help="Batch R: r1 single consume or r2 meta re-push")
    reuse_p.add_argument("--variant", default="r2", choices=["r1", "r2"])
    reuse_p.add_argument("--dump", help="path to save ticket row for r2")
    reuse_p.set_defaults(func=cmd_reuse)

    cross_p = sub.add_parser("cross-session", help="Batch C: mint in fresh subprocess")
    cross_p.add_argument("--round", type=int, default=1, choices=[1, 2])
    cross_p.set_defaults(func=cmd_cross_session)

    conc_p = sub.add_parser("concurrent", help="Batch M: N parallel image requests")
    conc_p.add_argument("--concurrency", "-n", type=int, default=2)
    conc_p.add_argument("--mint", action="store_true", help="mint until pool depth >= N")
    conc_p.add_argument("--require-tickets", action="store_true", help="mint exactly N tickets first")
    conc_p.set_defaults(func=cmd_concurrent)

    args = parser.parse_args()
    try:
        return args.func(args)
    except Exception as exc:
        log_event("batch_failed", cmd=args.cmd, error=str(exc)[:400])
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
