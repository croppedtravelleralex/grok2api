#!/usr/bin/env python3
"""Serial + concurrent validation with detailed probe; stop on first hard failure."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from chrome_ticket_experiment_lib import (  # noqa: E402
    DEFAULT_LOG,
    append_experiment_log,
    ensure_pool_depth,
    ensure_pin_scripts,
    log_event,
    mint_to_pool,
    pin_imagine_accounts,
    ssh_run,
    utc_now,
)
from chrome_ticket_survival_runner import detailed_probe, scp_probe  # noqa: E402


def fetch_imagine_accounts(limit: int) -> list[int]:
    proc = ssh_run(f"python3 /tmp/_panda_find_imagine.py --ids-only {limit}", timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return [int(x) for x in proc.stdout.strip().split(",") if x.strip()]


def export_sso(account_id: int, dest: Path) -> None:
    proc = subprocess.run(
        ["ssh", "panda", f"python3 /tmp/_panda_export_sso.py {account_id}"],
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"export sso {account_id}: {(proc.stderr or proc.stdout)[:200]}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(proc.stdout, encoding="utf-8")


def concurrent_probe(n: int, prompt: str) -> dict:
    scp_probe()
    script = f"""
import concurrent.futures, json, subprocess, time, urllib.error, urllib.request
KEY=open('/root/.secrets/grok2api-newapi-key').read().strip()
BASE='http://127.0.0.1:18000'
PROMPT={json.dumps(prompt)}
N={n}

def one(i):
    t0=time.time()
    body=json.dumps({{"model":"grok-imagine-image","prompt":PROMPT,"size":"1024x1024","n":1}}).encode()
    req=urllib.request.Request(BASE+'/v1/images/generations', data=body, method='POST',
        headers={{"Authorization":"Bearer "+KEY,"Content-Type":"application/json"}})
    http=0; url=''
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            http=resp.status
            data=json.loads(resp.read().decode()).get('data') or []
            if data: url=data[0].get('url') or ''
    except urllib.error.HTTPError as e:
        http=e.code
    return {{"idx":i,"http":http,"wall_ms":int((time.time()-t0)*1000),"url":(url or '')[:120]}}

with concurrent.futures.ThreadPoolExecutor(max_workers=N) as ex:
    rows=list(ex.map(one, range(N)))
logs=subprocess.run(['docker','logs','grok2api','--since','300s'], capture_output=True, text=True)
text=logs.stdout or ''
# bandwidth sample: download first 200 url if any
dl={{}}
for r in rows:
    if r.get('http')==200 and r.get('url'):
        t1=time.time()
        try:
            with urllib.request.urlopen(r['url'], timeout=60) as resp:
                b=resp.read(); el=max(time.time()-t1,1e-6)
                dl[str(r['idx'])]={{"bytes":len(b),"ms":int(el*1000),"mbps":round(len(b)*8/el/1e6,3)}}
        except Exception as e:
            dl[str(r['idx'])]={{"error":str(e)[:120]}}
        break
print(json.dumps({{"results":rows,"pool_hits":text.count('chrome_ticket_pool_hit'),"downloads":dl}}))
"""
    from chrome_ticket_experiment_lib import run_remote_python

    out = run_remote_python(script, remote_name="_validation_conc.py", timeout=max(600, n * 200))
    return json.loads(out.splitlines()[-1])


def run_serial(rounds: int, account_ids: list[int], sso_dir: Path, args) -> int:
    for r in range(1, rounds + 1):
        aid = account_ids[(r - 1) % len(account_ids)]
        sso = sso_dir / f"{aid}.json"
        if not sso.exists():
            export_sso(aid, sso)
        log_event("validation_serial", round=r, account_id=aid)
        ensure_pin_scripts()
        pin_imagine_accounts([aid])
        mint_to_pool(aid, sso, timeout=args.mint_timeout)
        probe = detailed_probe(log_since=300)
        ok = probe.get("http") == 200 and probe.get("pool_hit")
        append_experiment_log(
            f"V-serial-{r}",
            log_path=Path(args.log),
            round=r,
            account_id=aid,
            probe=probe,
            result="pass" if ok else "fail",
        )
        print(json.dumps({"round": r, "account_id": aid, "probe": probe}, ensure_ascii=False))
        if args.stop_on_fail and not ok:
            log_event("validation_stop", phase="serial", round=r, http=probe.get("http"))
            return 1
        time.sleep(args.gap_s)
    return 0


def run_concurrent(rounds: int, concurrency: int, account_ids: list[int], sso_dir: Path, args) -> int:
    for r in range(1, rounds + 1):
        aids = [account_ids[(r * concurrency + i) % len(account_ids)] for i in range(concurrency)]
        ensure_pin_scripts()
        pin_imagine_accounts(aids)
        for aid in set(aids):
            sso = sso_dir / f"{aid}.json"
            if not sso.exists():
                export_sso(aid, sso)
            ensure_pool_depth(aid, sso, 1, timeout=args.mint_timeout)
        log_event("validation_concurrent", round=r, concurrency=concurrency, accounts=aids)
        payload = concurrent_probe(concurrency, args.prompt)
        ok_count = sum(1 for x in payload["results"] if x.get("http") == 200)
        append_experiment_log(
            f"V-conc-{concurrency}-r{r}",
            log_path=Path(args.log),
            round=r,
            concurrency=concurrency,
            accounts=aids,
            payload=payload,
            http200=ok_count,
            result="pass" if ok_count >= min(concurrency, payload.get("pool_hits", 0)) else "fail",
        )
        print(json.dumps({"round": r, "payload": payload}, ensure_ascii=False))
        if args.stop_on_fail and ok_count < concurrency:
            log_event("validation_stop", phase="concurrent", round=r, http200=ok_count)
            return 1
        time.sleep(args.gap_s)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["serial", "concurrent", "all"], default="all")
    parser.add_argument("--serial-rounds", type=int, default=5)
    parser.add_argument("--conc-rounds", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--accounts", type=int, default=12, help="top imagine accounts to rotate")
    parser.add_argument("--log", default=str(ROOT / ".tmp" / "chrome-ticket-experiments.jsonl"))
    parser.add_argument("--mint-timeout", type=int, default=120)
    parser.add_argument("--gap-s", type=int, default=15)
    parser.add_argument("--prompt", default="a single red apple on white table, studio product photo")
    parser.add_argument("--stop-on-fail", action="store_true", default=True)
    args = parser.parse_args()

    ids = fetch_imagine_accounts(args.accounts)
    if not ids:
        print("no imagine accounts", file=sys.stderr)
        return 2
    sso_dir = ROOT / ".tmp" / "sso-batch"
    scp_export = subprocess.run(
        ["scp", str(TOOLS / "_panda_export_sso.py"), "panda:/tmp/_panda_export_sso.py"],
        capture_output=True,
    )
    if scp_export.returncode != 0:
        print("scp export failed", file=sys.stderr)
        return 2

    if args.phase in ("serial", "all"):
        rc = run_serial(args.serial_rounds, ids, sso_dir, args)
        if rc != 0:
            return rc
    if args.phase in ("concurrent", "all"):
        return run_concurrent(args.conc_rounds, args.concurrency, ids, sso_dir, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
