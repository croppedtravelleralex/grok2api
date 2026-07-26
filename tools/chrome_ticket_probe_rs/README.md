# Chrome ticket probe (Rust)

Unified pool probes with **single SSH** IO batch via `panda_unified_pool_snapshot.py`.

## Build

```bash
cd tools/chrome_ticket_probe_rs
cargo build --release
```

## Commands

```bash
# Full probe (dispatch + normal + ticket-quota + readiness)
tools/chrome_ticket_probe_rs/target/release/chrome_ticket_probe probe -c 5 -n 20 -w 16

# Ticket ↔ quota audit only
tools/chrome_ticket_probe_rs/target/release/chrome_ticket_probe audit -w 16

# Daemon JSONL (every 300s)
tools/chrome_ticket_probe_rs/target/release/chrome_ticket_probe daemon --interval 300 -c 5 -n 20

# Python wrapper (auto-prefers Rust binary)
python tools/chrome_ticket_pool_probe.py -c 5 -n 20 -w 16
```

## JIT preflight gate

```bash
python tools/chrome_ticket_jit_mint_daemon.py --audit-only -w 16
python tools/chrome_ticket_jit_mint_daemon.py --once --max-per-tick 5 -w 16
```

Preflight: unified snapshot → ticket_quota_audit → pin sync if orphans/pin drift → block violated accounts before mint.
