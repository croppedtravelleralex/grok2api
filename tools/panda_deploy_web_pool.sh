#!/usr/bin/env bash
set -euo pipefail
cd /opt/grok2api
TS=$(date +%Y%m%d%H%M%S)
cp .env ".env.bak.$TS"
cp docker-compose.yml "docker-compose.yml.bak.$TS"
NEW='ghcr.io/croppedtravelleralex/grok2api:codex-panda-safe-completion'
if grep -q '^GROK2API_IMAGE=' .env; then
  sed -i "s|^GROK2API_IMAGE=.*|GROK2API_IMAGE=$NEW|" .env
else
  echo "GROK2API_IMAGE=$NEW" >> .env
fi
echo "image=$(grep GROK2API_IMAGE .env)"
docker compose pull grok2api
docker compose up -d grok-signer grok2api
for i in $(seq 1 36); do
  sh=$(docker inspect grok-signer --format '{{.State.Health.Status}}' 2>/dev/null || echo starting)
  gh=$(docker inspect grok2api --format '{{.State.Health.Status}}' 2>/dev/null || echo starting)
  echo "attempt $i signer=$sh grok2api=$gh"
  if [ "$sh" = healthy ] && [ "$gh" = healthy ]; then
    break
  fi
  sleep 5
done
curl -fsS http://127.0.0.1:18000/healthz && echo
curl -fsS http://127.0.0.1:18000/readyz && echo
docker inspect grok2api --format 'status={{.State.Health.Status}} image={{.Config.Image}}'
