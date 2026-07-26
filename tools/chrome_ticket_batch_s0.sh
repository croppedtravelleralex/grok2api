#!/bin/bash
# Batch S0: single image attempt with pool stats (<=3min). Run on Panda or from repo root.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "python not found" >&2
  exit 1
fi

echo "=== pool stats (before) ==="
"$PY" tools/chrome_ticket_batch_run.py stats || true

MINT_FLAG=""
if [ "${CHROME_TICKET_S0_MINT:-}" = "1" ]; then
  MINT_FLAG="--mint"
fi

set +e
"$PY" tools/chrome_ticket_batch_run.py s0 $MINT_FLAG "$@"
RC=$?
set -e

echo "=== pool stats (after) ==="
"$PY" tools/chrome_ticket_batch_run.py stats || true

echo "=== recent experiment log ==="
"$PY" tools/chrome_ticket_experiment_log.py list --batch S0 --last 1 || true

exit "$RC"
