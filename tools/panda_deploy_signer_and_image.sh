#!/bin/bash
set -euo pipefail
cd /opt/grok2api
TS=$(date +%Y%m%d%H%M%S)
cp .env ".env.bak.$TS"
cp docker-compose.yml "docker-compose.yml.bak.$TS"
cp data/backend.db "backups/backend.db.bak.$TS"
echo "backups done $TS"

mkdir -p /root/.secrets
python3 <<'PY'
import json, os, stat
src = json.load(open("/tmp/zb_zero_keys.json"))
out = {"meta_b64": src["meta_b64"], "fingerprint": src["fingerprint"], "trailer_hex": "03"}
path = "/root/.secrets/grok-signer-pair"
with open(path, "w") as f:
    json.dump(out, f)
os.chmod(path, stat.S_IRUSR)
print("pair file ok")
PY

NEW='ghcr.io/croppedtravelleralex/grok2api@sha256:396d7b2cfd2e2de8d7a4fef0d8e4f3b302aa8d0e2adc10f612d5e1d7a2126b08'
if grep -q '^GROK2API_IMAGE=' .env; then
  sed -i "s|^GROK2API_IMAGE=.*|GROK2API_IMAGE=$NEW|" .env
else
  echo "GROK2API_IMAGE=$NEW" >> .env
fi
echo "image=$(grep GROK2API_IMAGE .env)"

python3 <<'PY'
import json, sqlite3
c = sqlite3.connect("/opt/grok2api/data/backend.db")
row = c.execute('select value_json from runtime_settings where key="gateway"').fetchone()
root = json.loads(row[0])
cfg = root["config"]
pw = cfg.setdefault("ProviderWeb", {})
pw["StatsigSignerURL"] = "http://grok-signer:8788/sign"
pw["StatsigMode"] = "url"
root["config"] = cfg
c.execute(
    'update runtime_settings set value_json=?, revision=revision+1, updated_at=datetime("now") where key="gateway"',
    (json.dumps(root, separators=(",", ":")),),
)
c.commit()
print("db signer url:", pw["StatsigSignerURL"])
PY

pkill -f '/tmp/zb_local_signer.py' 2>/dev/null || true
sleep 2

docker compose build grok-signer
docker compose pull grok2api
docker compose up -d grok-signer grok2api

echo "waiting health..."
for i in $(seq 1 36); do
  sh=$(docker inspect grok-signer --format '{{.State.Health.Status}}' 2>/dev/null || echo starting)
  gh=$(docker inspect grok2api --format '{{.State.Health.Status}}' 2>/dev/null || echo starting)
  echo "attempt $i signer=$sh grok2api=$gh"
  if [ "$sh" = healthy ] && [ "$gh" = healthy ]; then
    break
  fi
  sleep 5
done

echo "=== health checks ==="
curl -fsS http://127.0.0.1:8788/healthz && echo
curl -fsS http://127.0.0.1:8788/readyz && echo
curl -fsS http://127.0.0.1:18000/healthz && echo
curl -fsS http://127.0.0.1:18000/readyz && echo
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' | grep -E 'grok-signer|grok2api'
