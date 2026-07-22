# Grok Signer sidecar — Grok x-statsig-id signing service for grok2api.

## Overview

`grok-signer` is a minimal Python HTTP sidecar that signs Grok REST requests using a locked `meta48 + fingerprint` pair. It implements the same protocol as `backend/internal/infra/provider/web/statsig.go` (`POST /sign` with `method`, `path`, `environment.metaContent`).

The signer **ignores** `environment.metaContent` from grok2api (HTML verification meta ≠ signing meta). It always uses the pair loaded at startup.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /sign` | Return `{"x-statsig-id": "..."}` (70-byte base64 ticket) |
| `GET /healthz` | Process up, pair loaded, local sign produces valid 70B base64 |
| `GET /readyz` | L1 health + optional upstream probe via `SIGNER_PROBE_PROXY` |

## Environment

| Variable | Required | Description |
| --- | --- | --- |
| `SIGNER_PAIR_FILE` | one of pair sources | JSON file path, e.g. `/run/secrets/signer-pair` |
| `SIGNER_SEED_HEX` | with `SIGNER_HEX` | 48-byte meta as 96-char hex |
| `SIGNER_HEX` | with `SIGNER_SEED_HEX` | fingerprint string |
| `SIGNER_PORT` | no | listen port (default `8788`) |
| `SIGNER_PROBE_PROXY` | no | HTTP(S) proxy for `/readyz` upstream probe |
| `SIGNER_PROBE_BASE_URL` | no | default `https://grok.com` |
| `SIGNER_PROBE_PATH` | no | default `/rest/rate-limits` |
| `LOG_LEVEL` | no | default `INFO` |

### Pair file format

```json
{
  "meta_b64": "<48-byte grok-site-verification, base64>",
  "fingerprint": "<svg-derived hex fingerprint>",
  "trailer_hex": "03"
}
```

Compatible with `tools/pure_http_grok_runtime.py` `session_keys.json` (`meta_b64`, `fingerprint`).

**Do not commit pair files or env secrets.**

## grok2api config

```yaml
providerWeb:
  statsigMode: url
  statsigSignerURL: http://grok-signer:8788/sign
```

## Docker Compose (Panda)

`deploy/panda/docker-compose.yml` includes `grok-signer`; `grok2api` waits for `service_healthy`.

1. Place pair JSON on host, e.g. `/root/.secrets/grok-signer-pair`
2. Set `SIGNER_PROBE_PROXY` in `.env` if L2 readyz should probe grok.com (udeal egress)
3. `docker compose -f deploy/panda/docker-compose.yml up -d --build grok-signer grok2api`

## Local dev

```bash
# pair from env (test only)
export SIGNER_SEED_HEX=$(python -c "print(bytes(range(48)).hex())")
export SIGNER_HEX=testfingerprint

cd signer
python app.py
```

```bash
# unit tests
cd signer && python -m unittest -v

# smoke (signer must be running with real pair)
python smoke_test.py --base-url http://127.0.0.1:8788
```

## Build image

```bash
docker build -t grok-signer:latest signer/
```

## Algorithm

Signing logic matches `tools/pure_http_grok_runtime.py` `generate_statsig()`:

```
SHA256("{METHOD}!{PATH}!{counter}obfiowerehiring{fingerprint}")[:16]
XOR-encode meta48 + counter + digest + trailer
```

Extract pair once via `tools/panda_zb_x45.py` or `tools/pure_http_grok_runtime.py --extract`.
