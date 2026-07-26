# web_http_chat_image_canary_rs

Rust orchestrator for the frozen Python PoC.

| Layer | Tech |
|-------|------|
| Sign | frozen `web_http_chat_image_canary.v1.py` via `web_http_sign_helper.py` (Playwright) |
| HTTP | `web_http_post_worker.py` (curl_cffi chrome131) — preserves TLS fingerprint |
| Orchestration | Tokio: **A ∥ C** after sign; `--jobs N` runs N accounts in parallel |

Native `wreq` Chrome emulation was attempted but **btls-sys fails to build on this Windows host**; fingerprint stays in curl_cffi workers.

```bash
cargo run --release --manifest-path tools/web_http_chat_image_canary_rs/Cargo.toml -- \
  --sso-file .tmp/web-sso-canary.json --account-id 86

# two accounts in parallel
cargo run --release --manifest-path tools/web_http_chat_image_canary_rs/Cargo.toml -- \
  --sso-file .tmp/web-sso-canary.json --jobs 2
```
