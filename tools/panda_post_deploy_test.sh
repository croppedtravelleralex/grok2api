#!/bin/bash
set -euo pipefail
BASE=http://127.0.0.1:18000
PASS=$(cat /root/.secrets/grok2api-admin-password)
TOKEN=$(curl -fsS -X POST "$BASE/api/admin/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$PASS\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["tokens"]["accessToken"])')

echo "=== container health ==="
docker inspect grok-signer --format 'signer={{.State.Health.Status}} image={{.Config.Image}}'
docker inspect grok2api --format 'grok2api={{.State.Health.Status}} image={{.Image}}'

echo "=== endpoints ==="
curl -fsS "$BASE/healthz" && echo
curl -fsS "$BASE/readyz" && echo

echo "=== build-probe pools ==="
curl -fsS "$BASE/api/admin/v1/accounts/build-probe" -H "Authorization: Bearer $TOKEN" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin)["data"]; print("pools", d["pools"]); print("laneAttempts", d["statistics"].get("laneAttempts"))'

echo "=== accounts page 1 (count only) ==="
curl -fsS "$BASE/api/admin/v1/accounts?page=1&pageSize=5&provider=grok_build" -H "Authorization: Bearer $TOKEN" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin)["data"]; print("total", d["total"], "items", len(d["items"])); print("sample pools", [i.get("pool") for i in d["items"][:3]])'

echo "=== model_states table ==="
sqlite3 /opt/grok2api/data/backend.db "select count(*) from account_model_states;" 2>&1 || echo "table missing?"

echo "=== web account with modelStates ==="
curl -fsS "$BASE/api/admin/v1/accounts?page=1&pageSize=3&provider=grok_web" -H "Authorization: Bearer $TOKEN" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin)["data"]; 
for i in d["items"]:
  ms=i.get("modelStates") or []
  qw=[(w.get("mode"), w.get("remaining"), w.get("total")) for w in (i.get("quotaWindows") or []) if w.get("mode")=="imagine"]
  print(i.get("name"), "pool", i.get("pool"), "imagine", qw, "modelStates", len(ms))'

echo "=== single lite image (may 429) ==="
KEY=$(cat /root/.secrets/grok2api-newapi-key 2>/dev/null || echo "")
if [ -n "$KEY" ]; then
  START=$(date +%s)
  CODE=$(curl -sS -o /tmp/lite_test.json -w '%{http_code}' -X POST "$BASE/v1/images/generations" \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d '{"model":"grok-imagine-image","prompt":"a red apple on white background","size":"1024x1024","n":1}')
  END=$(date +%s)
  echo "http=$CODE wall=$((END-START))s"
  python3 -c 'import json; 
try:
 d=json.load(open("/tmp/lite_test.json"));
 print("keys", list(d.keys())[:5]);
 if "data" in d: print("url_prefix", (d["data"][0].get("url") or "")[:60])
except Exception as e: print("body", open("/tmp/lite_test.json").read()[:200])'
else
  echo "skip lite: no grok2api-newapi-key"
fi

echo "=== DONE ==="
