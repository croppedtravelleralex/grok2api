#!/bin/bash
# One-off: route imagine to account 1467 only, test Go ticket pool consumption, restore.
set -euo pipefail
BASE=http://127.0.0.1:18000
DB=/opt/grok2api/data/backend.db
TOKEN=$(python3 /tmp/_panda_admin_token.py)
KEY=$(cat /root/.secrets/grok2api-newapi-key)
BACKUP=/tmp/web_enabled_backup_$(date +%s).sql

echo "=== backup enabled flags ==="
sqlite3 "$DB" ".mode insert" ".output $BACKUP" "SELECT id,enabled FROM provider_accounts WHERE provider='grok_web';"
echo "backup rows: $(wc -l < $BACKUP)"

echo "=== disable all web except 1467 ==="
sqlite3 "$DB" "UPDATE provider_accounts SET enabled=0 WHERE provider='grok_web' AND id!=1467;"
sqlite3 "$DB" "SELECT id,enabled FROM provider_accounts WHERE id=1467;"

echo "=== pool before ==="
curl -fsS "$BASE/api/admin/v1/chrome-tickets/stats" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

echo "=== single image (account 1467 only) ==="
START=$(date +%s%3N)
CODE=$(curl -sS -o /tmp/img_1467.json -w '%{http_code}' -X POST "$BASE/v1/images/generations" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"grok-imagine-image","prompt":"a single red apple on white table","size":"1024x1024","n":1}')
END=$(date +%s%3N)
echo "http=$CODE wall_ms=$((END-START))"
python3 <<'PY'
import json
try:
 d=json.load(open('/tmp/img_1467.json'))
 if 'data' in d:
  print('url', (d['data'][0].get('url') or '')[:80])
  print('ok', True)
 elif 'error' in d:
  print('error', d['error'])
except Exception as e:
 print('raw', open('/tmp/img_1467.json').read()[:300])
PY

echo "=== pool after ==="
curl -fsS "$BASE/api/admin/v1/chrome-tickets/stats" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
sqlite3 "$DB" "SELECT id,account_id,status,consumed_at FROM chrome_tickets WHERE account_id=1467;"

echo "=== logs 1467 ==="
docker logs grok2api --since 3m 2>&1 | grep -E 'chrome_ticket_pool_hit|account_id.:1467|web_lite' | tail -20

echo "=== restore enabled flags ==="
sqlite3 "$DB" < "$BACKUP"
echo restored
