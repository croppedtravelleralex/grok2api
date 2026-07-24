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

当前结论（待跑 PoC 更新）：

- **P0–P1** 本机 curl_cffi 历史上可过（见 `pure_http_zero_browser_bootstrap.py`）
- **P2–P3** 仍依赖设备 cookie / 票池；纯 HTML meta ** alone 不足以稳定生产**
- 若 PoC 稳定 P0+P1，可进 Phase-2：meta 入池替代 Chrome minter（仍要本机或 signer 对齐 fp）

## 相关

- [13-chrome-ticket-pool-panda-api](./13-chrome-ticket-pool-panda-api-2026-07-23.md)
- [http-reverse-lite-chain](./http-reverse-lite-chain.md)
- BE-008 backlog
