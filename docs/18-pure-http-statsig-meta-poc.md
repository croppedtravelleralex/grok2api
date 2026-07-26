# Grok 纯 HTTP statsig_meta PoC（2026-07-24）

> 验证能否像 gptimage/chatgpt2api 一样**不启动 Chrome**，仅用 `curl_cffi` 过 CF 并拿到可用于生图的 `statsig_meta`。

## 与 chatgpt2api 的差异

| 层 | chatgpt2api (gptimage) | Grok |
|----|------------------------|------|
| CF | VM Turnstile + `chat-requirements` | HTML `grok-site-verification` meta |
| 短票 | sentinel token | `x-statsig-id`（~45s，需 signer 或算法重建） |
| 设备指纹 | 较少强调 | `grok_device_id` / `x-userid` cookie |
| 生图 | `backend-api` files/tasks | `/rest/app-chat` SSE + Lite asset |

**不能复用** ChatGPT 的 Turnstile 链；必须单独验证 Grok HTML meta 路径。

## 实验脚本

```bash
python tools/pure_http_statsig_meta_poc.py \
  --sso-file .tmp/web-sso-pin4.json \
  --account-id 1574 \
  --repeat 3 \
  --lite
```

### 阶段矩阵

| 阶段 | 通过标准 | 失败含义 |
|------|----------|----------|
| P0 首页 | HTTP 200 + meta 48 字节 | CF challenge / 无 meta |
| P1 Chat | `conversation` in body | anti-bot / 签名错 |
| P2 Lite | SSE 含 image 标记 | 额度 / soft_stop |
| P3 Asset | `assets.grok.com` 200 | 无票设备 cookie / 出口 |

产物：`.tmp/pure-http-statsig-poc/poc-*.json`、`aggregate.json`

## 生产可行性门禁

| 门禁 | 阈值 |
|------|------|
| meta 提取成功率 | ≥ 95% / 10 次（同出口） |
| chat_ok 率 | ≥ 90% |
| Lite E2E（含下载） | ≥ 80%（需 Panda udeal + 票池或设备 cookie） |
| 无 Chrome 内存 | Panda 零 browser-bridge |

当前结论（2026-07-24 Panda udeal LA `70.39.164.200:30000`）：

| 路径 | meta48 | chat | Lite | 说明 |
|------|--------|------|------|------|
| `pure_http_statsig_meta_poc`（HTML 现抓） | **3/3** | **0/3** anti-bot 403 | **0/3** 403 | 仅有 meta，无匹配 fp/signer |
| `panda_zero_browser_http --mode page` | ✅ 48B | — | ❌ no_svg | 页面无 SVG 指纹 |
| `panda_zero_browser_http --mode anon` | — | — | — | 缺 `coincurve` 依赖 |
| `panda_zero_browser_http --mode reuse`（`/tmp/session_keys.json`） | ✅ | 200 | **200 image_ok ~1.0s** | **需历史 Chrome 捕获的 meta+fp 对** |

**结论**：udeal 出口上 **纯 curl_cffi 可过 CF 并抽 meta**，但 **不能**像 gptimage 那样「只 TLS 伪装 + Turnstile」即用；Grok 还需 **meta48 + fingerprint 配对**（来自 SPA/Chrome 捕获）。`reuse` 模式证明配对正确时 **零浏览器 Lite 200**；这与生产 **开票池**（预存 meta+device cookie）方向一致，但 HTML 单抓 meta 不足以替代 Chrome 开票。

Panda 日志：`/tmp/poc-a.log`、`/tmp/poc-reuse.json`

## 相关

- [13-chrome-ticket-pool-panda-api](./13-chrome-ticket-pool-panda-api-2026-07-23.md)
- [http-reverse-lite-chain](./http-reverse-lite-chain.md)
- BE-008 backlog
