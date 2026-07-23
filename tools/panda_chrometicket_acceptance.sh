#!/bin/bash
set -euo pipefail
BASE=http://127.0.0.1:18000
PASS=$(cat /root/.secrets/grok2api-admin-password)
TOKEN=$(curl -fsS -X POST "$BASE/api/admin/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$PASS\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["tokens"]["accessToken"])')

echo "=== health ==="
curl -fsS "$BASE/healthz" && echo
curl -fsS "$BASE/readyz" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("ready", d.get("ready", d))'

echo "=== chrome ticket pool stats ==="
curl -fsS "$BASE/api/admin/v1/chrome-tickets/stats" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

echo "=== imagine quota top5 ==="
sqlite3 /opt/grok2api/data/backend.db <<'SQL'
.mode column
.headers on
SELECT pa.id, pa.name, qw.remaining, qw.total
FROM provider_accounts pa
JOIN account_quota_windows qw ON qw.account_id=pa.id
WHERE pa.provider='grok_web' AND pa.enabled=1 AND qw.mode='imagine' AND qw.remaining>0
ORDER BY qw.remaining DESC LIMIT 5;
SQL

echo "=== account 1467 imagine ==="
sqlite3 /opt/grok2api/data/backend.db "SELECT remaining,total FROM account_quota_windows WHERE account_id=1467 AND mode='imagine';"

echo "=== lite image test (no pool) ==="
KEY=$(cat /root/.secrets/grok2api-newapi-key 2>/dev/null || echo "")
if [ -z "$KEY" ]; then
  echo "skip: no client key"
  exit 0
fi
START=$(date +%s%3N)
CODE=$(curl -sS -o /tmp/lite_accept.json -w '%{http_code}' -X POST "$BASE/v1/images/generations" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"grok-imagine-image","prompt":"a single red apple on white table, product photo","size":"1024x1024","n":1}')
END=$(date +%s%3N)
WALL=$((END-START))
echo "http=$CODE wall_ms=$WALL"
python3 <<'PY'
import json, os
p='/tmp/lite_accept.json'
raw=open(p,'rb').read()
print('body_bytes', len(raw))
try:
 d=json.load(open(p))
 if 'data' in d and d['data']:
  url=d['data'][0].get('url','')
  print('url_prefix', url[:80])
  print('revised_prompt', (d['data'][0].get('revised_prompt') or '')[:60])
 elif 'error' in d:
  print('error', d['error'])
 else:
  print('keys', list(d.keys())[:8])
except Exception as e:
 print('raw', raw[:300])
PY

echo "=== recent grok2api logs (ticket/chrome) ==="
docker logs grok2api --since 5m 2>&1 | grep -E 'chrome_ticket|pool_hit|soft_stop|lite' | tail -20 || true

echo "=== chrome ticket pool stats (after) ==="
curl -fsS "$BASE/api/admin/v1/chrome-tickets/stats" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

echo "=== DONE ==="
