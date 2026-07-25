#!/usr/bin/env bash
set -euo pipefail
cd /opt/grok2api
cp -a .env ".env.bak.$(date +%Y%m%d%H%M%S)"
echo "OLD_IMAGE=$(grep -E '^GROK2API_IMAGE=' .env | cut -d= -f2-)"
docker pull ghcr.io/croppedtravelleralex/grok2api:codex-panda-safe-completion
NEW_DIGEST=$(docker image inspect ghcr.io/croppedtravelleralex/grok2api:codex-panda-safe-completion --format='{{index .RepoDigests 0}}' | sed 's/.*@//')
echo "NEW_DIGEST=$NEW_DIGEST"
NEW_IMAGE="ghcr.io/croppedtravelleralex/grok2api@${NEW_DIGEST}"
sed -i "s|^GROK2API_IMAGE=.*|GROK2API_IMAGE=${NEW_IMAGE}|" .env
docker compose pull grok2api
docker compose up -d grok2api
sleep 10
docker inspect grok2api --format='health={{.State.Health.Status}}'
curl -fsS http://127.0.0.1:18000/healthz && echo
