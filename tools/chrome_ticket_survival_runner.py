#!/usr/bin/env python3
"""One survival tier: mint dedicated ticket → wait → detailed probe. Stop-on-fail."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from chrome_ticket_experiment_lib import (  # noqa: E402
    DEFAULT_LOG,
    DEFAULT_SSO,
    append_experiment_log,
    ensure_pin_scripts,
    log_event,
    mint_to_pool,
    parse_duration,
    pin_imagine_accounts,
    run_remote_python,
    sleep_countdown,
    ssh_run,
    utc_now,
    verdict,
)

PROBE_REMOTE = "_panda_image_probe.py"
STATE_DIR = Path(__file__).resolve().parents[1] / ".tmp" / "survival-state"


def scp_probe() -> None:
    local = TOOLS / "_panda_image_probe.py"
    subprocess_run = __import__("subprocess").run
    host = __import__("os").environ.get("PANDA_SSH", "panda")
    proc = subprocess_run(["scp", str(local), f"{host}:/tmp/_panda_image_probe.py"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "scp probe failed")[:300])


def detailed_probe(*, log_since: int, account_id: int | None = None, pin: bool = False) -> dict:
    scp_probe()
    if pin and account_id:
        ensure_pin_scripts()
        pin_imagine_accounts([account_id])
    proc = ssh_run(f"python3 /tmp/_panda_image_probe.py --log-since {log_since}", timeout=240)
    if proc.returncode != 0:
        raise RuntimeError(f"probe failed: {(proc.stderr or proc.stdout)[:400]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def save_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def run_tier(args: argparse.Namespace) -> int:
    age_s = parse_duration(args.age)
    batch = args.batch or f"S-{args.age}"
    state_path = Path(args.state or (STATE_DIR / f"{batch.replace('/', '-')}.json"))
    ttl = max(args.ttl_hours, age_s / 3600 + 2)

    if args.phase == "consume":
        state = load_state(state_path)
        minted = {"pushed": {"id": state["ticket_id"]}, "minted_at": state["minted_at"]}
        log_event("survival_resume", batch=batch, state=str(state_path))
        ensure_pin_scripts()
        pin_imagine_accounts([args.account_id])
        # Long tiers may mark the pool row expired so short probes cannot pop it;
        # re-push saved meta (or revive expired row) before consume probe.
        row = state.get("ticket_row")
        if isinstance(row, dict) and (row.get("statsig_meta") or ""):
            from chrome_ticket_experiment_lib import push_ticket

            pushed = push_ticket(row, ttl_hours=ttl)
            minted["pushed"] = pushed
            log_event("survival_repush", batch=batch, ticket_id=pushed.get("id"))
        else:
            ssh_run(
                "python3 - <<'PY'\n"
                "import sqlite3\n"
                f"db=sqlite3.connect('/opt/grok2api/data/backend.db')\n"
                f"db.execute(\"UPDATE chrome_tickets SET status='available', consumed_at=NULL "
                f"WHERE id=? AND status='expired'\", ({state['ticket_id']!r},))\n"
                "db.commit()\n"
                "print(db.execute('select status from chrome_tickets where id=?', "
                f"({state['ticket_id']!r},)).fetchone())\n"
                "PY",
                timeout=30,
            )
    else:
        ensure_pin_scripts()
        pin_imagine_accounts([args.account_id])
        minted = mint_to_pool(args.account_id, Path(args.sso_file), timeout=args.mint_timeout, ttl_hours=ttl)
        save_state(
            state_path,
            {
                "batch": batch,
                "account_id": args.account_id,
                "ticket_id": minted["pushed"].get("id"),
                "minted_at": minted["minted_at"],
                "age_s": age_s,
                "consume_after": utc_now(),
            },
        )
        if args.phase == "mint":
            log_event("survival_mint_only", batch=batch, state=str(state_path))
            print(json.dumps(load_state(state_path), ensure_ascii=False))
            return 0
        sleep_countdown(age_s, label=f"survival-{args.age}")

    probe = detailed_probe(log_since=max(age_s + 120, 300))
    result = verdict(probe.get("http", 0), probe.get("pool_hit", False))
    row = append_experiment_log(
        batch,
        log_path=Path(args.log),
        account_id=args.account_id,
        ticket_id=minted["pushed"].get("id"),
        minted_at=minted.get("minted_at"),
        ticket_age_s=age_s,
        ttl_hours=ttl,
        result=result,
        probe=probe,
    )
    print(json.dumps(row, ensure_ascii=False))
    if args.stop_on_fail and result != "pass":
        log_event("survival_stop", batch=batch, reason=result, http=probe.get("http"))
        return 1
    return 0 if result == "pass" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Survival tier: one ticket, wait, probe")
    parser.add_argument("--age", required=True, help="10m, 15m, 30m, 60m, 3h, 6h, 12h")
    parser.add_argument("--batch", help="log batch id")
    parser.add_argument("--account-id", type=int, default=1467)
    parser.add_argument("--sso-file", default=str(DEFAULT_SSO))
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--ttl-hours", type=float, default=24.0)
    parser.add_argument("--mint-timeout", type=int, default=120)
    parser.add_argument("--phase", choices=["full", "mint", "consume"], default="full")
    parser.add_argument("--state", help="state json for consume-only resume")
    parser.add_argument("--stop-on-fail", action="store_true", default=True)
    parser.add_argument("--no-stop-on-fail", action="store_false", dest="stop_on_fail")
    args = parser.parse_args()
    try:
        return run_tier(args)
    except Exception as exc:
        log_event("survival_failed", age=args.age, error=str(exc)[:400])
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
