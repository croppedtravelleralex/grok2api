#!/bin/bash
set -euo pipefail
BASE=http://127.0.0.1:18000
TOKEN=$(python3 /tmp/_panda_admin_token.py)

echo "=== pool stats ==="
curl -fsS "$BASE/api/admin/v1/chrome-tickets/stats" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

echo "=== available tickets detail ==="
sqlite3 /opt/grok2api/data/backend.db <<'SQL'
.mode column
.headers on
SELECT id, account_id, substr(statsig_meta,1,20) AS meta_prefix, status, expires_at
FROM chrome_tickets ORDER BY created_at DESC LIMIT 5;
SQL

KEY=$(cat /root/.secrets/grok2api-newapi-key)
PROMPT='a single red apple on white table, studio product photo'

echo "=== image attempts (up to 8, watch pool_hit) ==="
for n in $(seq 1 8); do
  docker logs grok2api --since 1s >/dev/null 2>&1 || true
  START=$(date +%s%3N)
  CODE=$(curl -sS -o /tmp/img_try_$n.json -w '%{http_code}' -X POST "$BASE/v1/images/generations" \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"grok-imagine-image\",\"prompt\":\"$PROMPT\",\"size\":\"1024x1024\",\"n\":1}")
  END=$(date +%s%3N)
  HIT=$(docker logs grok2api --since 30s 2>&1 | grep -c 'chrome_ticket_pool_hit' || true)
  ACCT=$(docker logs grok2api --since 30s 2>&1 | grep 'chrome_ticket_pool_hit' | tail -1 | python3 -c 'import sys,re; s=sys.stdin.read();
import json
m=re.search(r"account_id\"?[:, ]+(\d+)", s)
print(m.group(1) if m else "-")' 2>/dev/null || echo "-")
  BYTES=$(wc -c < /tmp/img_try_$n.json)
  echo "try=$n http=$CODE wall_ms=$((END-START)) body_bytes=$BYTES pool_hits_recent=$HIT last_hit_account=$ACCT"
  if [ "$CODE" = "200" ]; then
    python3 -c "import json; d=json.load(open('/tmp/img_try_$n.json')); print('url', (d.get('data',[{}])[0].get('url') or '')[:70])" 2>/dev/null || true
    break
  fi
  sleep 2
done

echo "=== pool stats after ==="
curl -fsS "$BASE/api/admin/v1/chrome-tickets/stats" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

echo "=== ticket rows after ==="
sqlite3 /opt/grok2api/data/backend.db "SELECT id,account_id,status,consumed_at FROM chrome_tickets ORDER BY created_at DESC LIMIT 5;"

echo "=== relevant logs ==="
docker logs grok2api --since 10m 2>&1 | grep -E 'chrome_ticket_pool_hit|web_lite_image_not_found|web_lite_asset_download|upstream_unavailable' | tail -30
