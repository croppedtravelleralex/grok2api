#!/usr/bin/env python3
"""Run V-serial / V-conc using accounts currently in imagePoolIds."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from chrome_ticket_experiment_lib import (  # noqa: E402
    append_experiment_log,
    ensure_pin_scripts,
    ensure_pool_depth,
    log_event,
    pin_imagine_accounts,
    ssh_run,
)
from chrome_ticket_survival_runner import detailed_probe  # noqa: E402
from chrome_ticket_validation_runner import concurrent_probe, export_sso  # noqa: E402


def image_pool_ids(limit: int = 12) -> list[int]:
    local = TOOLS / "_panda_image_pool_ids.py"
    subprocess.run(
        ["scp", str(local), "panda:/tmp/_panda_image_pool_ids.py"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    proc = ssh_run(f"python3 /tmp/_panda_image_pool_ids.py {limit}", timeout=60)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "image_pool_ids failed")[:400])
    line = proc.stdout.strip().splitlines()[-1]
    return [int(x) for x in line.split(",") if x.strip().isdigit()]


def restart_grok2api() -> None:
    """Rebuild in-memory webImageLane.dispatch so route pin ∩ index is non-empty."""
    proc = ssh_run(
        "cd /opt/grok2api && docker compose restart grok2api && "
        "for i in 1 2 3 4 5 6 7 8 9 10; do "
        "curl -fsS -m 2 http://127.0.0.1:18000/healthz >/dev/null 2>&1 && exit 0; "
        "sleep 2; done; exit 1",
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"restart grok2api failed: {(proc.stderr or proc.stdout)[:300]}")
    print("restarted grok2api (web pool index rebuilt)", flush=True)


def ticket_ready_ids(prefer: list[int], *, min_count: int = 3) -> list[int]:
    """Prefer image-pool ids that already have available tickets; always top-up from other ticketed ids."""
    from chrome_ticket_experiment_lib import pool_stats

    stats = pool_stats()
    by_acct = {
        int(r.get("AccountID") or 0): int(r.get("Count") or 0)
        for r in (stats.get("AvailableByAccount") or [])
        if int(r.get("AccountID") or 0) > 0 and int(r.get("Count") or 0) > 0
    }
    # 1467 historically L3-rate-limits; keep it out of V cohort unless it is the only option.
    skip = {1467}
    preferred = [aid for aid in prefer if by_acct.get(aid, 0) > 0 and aid not in skip]
    extras = [
        aid
        for aid, _ in sorted(by_acct.items(), key=lambda x: -x[1])
        if aid not in preferred and aid not in skip
    ]
    out = preferred + extras
    if not out:
        # last resort include skipped
        out = [aid for aid, _ in sorted(by_acct.items(), key=lambda x: -x[1])]
    if not out:
        raise RuntimeError("no available chrome tickets for any account")
    if len(out) > max(min_count, 8):
        out = out[: max(min_count, 8)]
    return out


def resolve_sso(aid: int, sso_dir: Path) -> Path:
    sso = sso_dir / f"{aid}.json"
    if sso.exists():
        return sso
    for cand in (
        ROOT / ".tmp" / f"web-sso-canary-{aid}.json",
        ROOT / ".tmp" / "web-sso-canary-1574.json",
        ROOT / ".tmp" / "web-sso-canary-1507.json",
    ):
        if cand.exists():
            # canary files are multi-account; export single-id file when possible
            try:
                export_sso(aid, sso)
                if sso.exists():
                    return sso
            except Exception:
                pass
    export_sso(aid, sso)
    return sso


def main() -> int:
    phase = sys.argv[1] if len(sys.argv) > 1 else "all"
    pool_ids = image_pool_ids(12)
    print("image pool ids", pool_ids, flush=True)
    ids = ticket_ready_ids(pool_ids, min_count=1)
    print("using ticket-ready ids", ids, flush=True)
    sso_dir = ROOT / ".tmp" / "sso-batch"
    sso_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["scp", str(TOOLS / "_panda_export_sso.py"), "panda:/tmp/_panda_export_sso.py"],
        check=False,
    )
    ensure_pin_scripts()

    # Pin full V cohort once, then restart so every id is in rebuilt dispatch index.
    cohort = list(dict.fromkeys(ids))
    pin_imagine_accounts(cohort)
    restart_grok2api()

    if phase in ("serial", "all"):
        for r in range(1, 6):
            aid = ids[(r - 1) % len(ids)]
            sso = resolve_sso(aid, sso_dir)
            log_event("validation_serial", round=r, account_id=aid)
            # Prefer existing tickets; only mint when depth is zero.
            try:
                ensure_pool_depth(aid, sso, 1, timeout=120)
            except Exception as exc:
                print(f"ensure_pool_depth warn aid={aid}: {exc}", flush=True)
            probe = detailed_probe(log_since=300)
            ok = probe.get("http") == 200 and probe.get("pool_hit")
            append_experiment_log(
                f"V-serial-{r}",
                round=r,
                account_id=aid,
                probe=probe,
                result="pass" if ok else "fail",
            )
            print(json.dumps({"round": r, "account_id": aid, "http": probe.get("http"), "pool_hit": probe.get("pool_hit"), "wall_ms": probe.get("wall_ms")}, ensure_ascii=False), flush=True)
            if not ok:
                log_event("validation_stop", phase="serial", round=r, http=probe.get("http"))
                return 1
            time.sleep(15)

    if phase in ("concurrent", "all"):
        for r in range(1, 4):
            conc = 10
            aids = [ids[(r * conc + i) % len(ids)] for i in range(conc)]
            pin_imagine_accounts(list(dict.fromkeys(cohort)))
            for aid in set(aids):
                sso = resolve_sso(aid, sso_dir)
                try:
                    ensure_pool_depth(aid, sso, max(1, conc // max(len(set(aids)), 1)), timeout=120)
                except Exception as exc:
                    print(f"ensure_pool_depth warn aid={aid}: {exc}", flush=True)
            # Need enough total tickets for concurrency.
            from chrome_ticket_experiment_lib import pool_available

            avail = pool_available()
            if avail < conc:
                print(f"warn: only {avail} tickets available for conc={conc}", flush=True)
            log_event("validation_concurrent", round=r, concurrency=conc, accounts=aids)
            payload = concurrent_probe(conc, "a single red apple on white table, studio product photo")
            ok_count = sum(1 for x in payload["results"] if x.get("http") == 200)
            append_experiment_log(
                f"V-conc-{conc}-r{r}",
                round=r,
                concurrency=conc,
                accounts=aids,
                payload=payload,
                http200=ok_count,
                result="pass" if ok_count >= conc else "fail",
            )
            print(json.dumps({"round": r, "http200": ok_count, "pool_hits": payload.get("pool_hits"), "results": payload.get("results")}, ensure_ascii=False), flush=True)
            if ok_count < conc:
                log_event("validation_stop", phase="concurrent", round=r, http200=ok_count)
                return 1
            time.sleep(15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
