# Web Lite 生图两阶段失败与 asset 下载 403（2026-07-23）

> 运维/排障事实档。记录 Panda 生产上「有额度、udeal 可用，但生图仍失败」的根因分层与证据。

## 结论摘要

| 项 | 状态 |
|----|------|
| 代理（udeal LA `70.39.164.200:30000`）能连 `grok.com` | ✅ SSE 可收到 20KB+ 流 |
| 账面 imagine 额度（micro-credit） | ✅ 与「能否出图」无必然关系 |
| **阶段 1** SSE 出 URL | ⚠️ 大量 `isSoftStop=true` + `image_chunks=0` |
| **阶段 2** `assets.grok.com` 下载 | ⚠️ 近期 SSE 成功后频繁 **403**（非稳定 100%） |
| staged soft_stop 只试 1 次 | ✅ 已修（`971e997`，最多 8 次换号） |
| asset 下载 403 换出口重试 | ❌ 未做（当前 403 直接 502） |

**一句话**：有额度和 udeal 只保证「能连上、能查额度」；生图失败主要是上游 **soft_stop** 或 **CDN 下载 403**，不是代理完全不可用。

## 两阶段链路

```text
客户端 POST /v1/images/generations (grok-imagine-image)
        │
        ▼
ImagePipelineScheduler（staged：PS 扩写 → SS SSE）
        │  ScopeWeb：udeal-la-grok_web（70.39.164.200:30000）
        ▼
POST /rest/app-chat/conversations/new
  message = "Drawing: …"
  enableImageGeneration = true
        │
        ├─ 失败 A：SSE 结束 isSoftStop=true，无 image_chunk → soft_stop（换号重试）
        │
        └─ 成功：解析出 https://assets.grok.com/... URL
                │
                ▼
           downloadImage()
                │  ScopeWebAsset：udeal-la-grok_web_asset（同一 IP）
                ▼
           ├─ 失败 B：HTTP 403 → 502 upstream_unavailable
           └─ 成功：落盘 media → 返回 grokimage.relai.asia URL
```

代码入口：

- SSE：`backend/internal/infra/provider/web/image.go` → `generateLiteImageURL` → `openChat`
- 下载：同文件 → `downloadImage` → `egress.Acquire(ScopeWebAsset)`

## Panda 生产出口（2026-07-23 核实）

| 节点 | scope | 代理 | enabled |
|------|--------|------|---------|
| `udeal-la-grok_web` | `grok_web` | `70.39.164.200:30000` | **唯一** |
| `udeal-la-grok_web_asset` | `grok_web_asset` | **同上 IP** | **唯一** |
| webshare `grok_web` ×34 | `grok_web` | — | 全部禁用 |

516 个 Web 账号共用 **1 个** API 出口 + **1 个** asset 出口。维护探针对单账号持续 `429 Too many requests`。

## 失败模式与日志关键字

### A. SSE soft_stop（多数）

```
web_lite_image_not_found  soft_stop=true  image_chunks=0  max_progress=0
image_upstream_soft_stop    attempt=N  staged=true
```

含义：上游 HTTP 200，流正常结束，但 Grok 标记 `isSoftStop` 且未下发任何 `image_chunk`。

### B. asset 下载 403（SSE 已成功）

```
image_upstream_failed  error="下载图片返回 403"  staged=true
```

**2026-07-23 连续实测**（grok2api 生产路径）：

| 账号 | 结果 |
|------|------|
| 241, 349 | SSE 成功 → 下载 403 |
| 369, 382, 342 | 同上（连续 3 次 bench） |

**反例（说明非永久封 IP）**：账号 **1385** 曾多次 `succeeded`，`download_ms` 865–1598ms，走同一条 udeal-la asset 出口。

因此 403 更像 **URL/SSO/会话绑定 + 单 IP 风控**，而非 CDN 对 udeal IP 永久拉黑。

## 额度与可调度

- 上游 `imagine.remaining=3850000000` 为 micro-credit，换算约 **10 次/账号**（见 `ImagineGenerations`）。
- 面板「账面 194 账号 / 1920 次」≠ 当前一定能出图。
- bench 连续失败后 `imageSchedulableAccounts` 可从 156 降至 57（soft_stop 冷却）。

## 探针与复现

| 路径 | 用途 |
|------|------|
| `tools/panda_asset_download_probe.py` | 拿真实 URL 后做 proxy/cookie 下载矩阵 |
| `tools/panda_web_serial_image_bench.py` | 串行 bench，fail-fast |
| 日志 `asset_url_tail` | **2026-07-23 起** 下载失败时由 `downloadImage` 打出 URL 尾缀，供 `ASSET_URL=… ASSET_ACCOUNT_ID=…` 离线矩阵 |

```bash
# Panda 上仅测下载矩阵（需先从日志复制 asset_url_tail 拼完整 URL）
ASSET_URL='https://assets.grok.com/users/.../image.jpg' \
ASSET_ACCOUNT_ID=349 \
USE_GROK2API_URL=0 \
python3 /opt/grok2api/tools/panda_asset_download_probe.py
```

## 待办（建议）

1. **增加 `grok_web_asset` 出口**（与 API 口分离 IP），下载 403 时换 asset 节点重试。
2. **下载失败纳入重试策略**（当前仅 soft_stop 换号，403 不重试）。
3. **扩 udeal sticky 池**，避免 500+ 账号挤单 IP。

## 相关文档

- [07-udeal-zero-browser-ops-2026-07-21.md](./07-udeal-zero-browser-ops-2026-07-21.md) — Panda 零浏览器签名 + udeal 生产事实
- [09-imagine-quota-model-state-and-10-concurrency-todos-2026-07-22.md](./09-imagine-quota-model-state-and-10-concurrency-todos-2026-07-22.md) — 额度/模型状态/流水线
- [http-reverse-lite-chain.md](./http-reverse-lite-chain.md) — **冻结** Chrome 开票 + HTTP Lite 开发链路（见下节）

---

## 附录：Chrome 开票 + HTTP 生图链路（历史冻结 vs 当前生产）

### A. 冻结开发链路（本机 / canary，**含 Chrome 短签**）

主文档：**[http-reverse-lite-chain.md](./http-reverse-lite-chain.md)**

```text
Mihomo 127.0.0.1:7897（或 udeal）
        │
        ├─ B  Playwright channel=chrome（仅短签，~9–40s）
        │     注入 SSO → grok.com → 捕获 x-statsig-id + cf_clearance
        │
        ├─ C  curl_cffi POST /rest/app-chat/conversations/new（PONG 探针）
        │
        └─ D  同上，Drawing: … + enableImageGeneration → assets.grok.com URL
              curl_cffi GET 下图（须带 SSO Cookie）
```

| 角色 | 文件 |
|------|------|
| **正式入口（冻结）** | `tools/web_chrome_http_lite_frozen.py` |
| 实现库 | `tools/_chrome_channel_chat_image.py` |
| HTTP / 下载助手 | `tools/web_http_chat_image_canary.v1.py` |
| 指针入口 | `tools/web_http_chat_image_canary.py` |
| Panda bridge 版 canary | `tools/panda_chrome_sign_http_canary.py` |
| 两轮性能 | `tools/_chrome_lite_bench.py` |
| SSO 导出 | `tools/panda_export_fresh_web_sso.py` / `panda_export_free_web_sso.py` |
| 账号池 JSON | `.tmp/web-sso-canary.json` |

复跑（Windows 本机）见 `http-reverse-lite-chain.md` §复跑。

**边界**：Lite REST **无** `aspect_ratio`；16:9/2K 走 Imagine WebSocket → `tools/_imagine_once.py`（另一条链）。

### B. Panda 生产链路（**无 Chrome 常驻**，2026-07-21 起）

主文档：**[07-udeal-zero-browser-ops-2026-07-21.md](./07-udeal-zero-browser-ops-2026-07-21.md)**

```text
grok2api 容器
        │
        ├─ GROK2API_BROWSER_BRIDGE_URL=（空）  ← 必须空，否则强制走 Chromium
        │
        ├─ 本地签名器 127.0.0.1:8788/sign（nsenter 共享 netns）
        │     零浏览器抽钥 + 每请求现算 x-statsig-id
        │     脚本：/tmp/zb_local_signer.py、/tmp/start_signer_nsenter.sh
        │
        ├─ curl_cffi / Go HTTP → grok.com/rest/…（ScopeWeb = udeal-la）
        │
        └─ Lite SSE → downloadImage（ScopeWebAsset = udeal-la，同 IP）
```

| 角色 | 位置 |
|------|------|
| 零浏览器 e2e 验证 | `tools/panda_zb_x45.py` |
| 网关 Lite 实现 | `backend/internal/infra/provider/web/image.go` |
| 签名 | `backend/internal/infra/provider/web/statsig.go` + 本地 `/sign` |
| 流水线调度 | `backend/internal/application/imagepipeline/` |
| bridge（**生产已关**） | `backend/internal/infra/provider/web/browser_bridge.go` |

### D. 本机 Chrome 开票 → Panda 用票出图（2026-07-23，E2E 已验收）

Panda 不适合跑 Chrome；本机短签 **`statsig_meta`** 后送 Panda，由 **`grok-signer`** 现签 + udeal 纯 HTTP Lite。

```text
本机 Windows + Google Chrome (headed, Playwright channel=chrome)
        │  捕获 statsig_meta + grok_device_id（~90s/账号）
        ▼
SSH ticket v2 JSON 或写入票池 (panda_ticket_pool.py)
        ▼
Panda: panda_lite_with_ticket.py / panda_ticket_image_api.py
        │  grok-signer 172.22.0.2:8788 → 新鲜 x-statsig-id
        │  egress warm + udeal ScopeWeb → Lite SSE
        │  udeal ScopeWebAsset → 下载 assets.grok.com
        ▼
    回图（/tmp/... 或 SCP 回本机 .tmp/lite-images/）
```

| 角色 | 文件 |
|------|------|
| 单次 E2E 编排 | `tools/local_chrome_panda_lite.py` |
| Panda Lite worker | `tools/panda_lite_with_ticket.py` |
| 票池（Go） | `chrome_tickets` + Admin API |
| 批量入池 | `tools/chrome_ticket_pool_minter.py` |
| 完整设计 | [13-chrome-ticket-pool-panda-api-2026-07-23.md](./13-chrome-ticket-pool-panda-api-2026-07-23.md) |
| 生命周期实验 | [14-chrome-ticket-lifecycle-experiments-2026-07-23.md](./14-chrome-ticket-lifecycle-experiments-2026-07-23.md) |

```powershell
# 导出 SSO（账号须有 imagine 额度，1468 为 0 会 1010）
ssh panda "python3 /tmp/_panda_export_sso.py 1467" > .tmp\web-sso-canary-1467.json

# 单次 E2E（票即用 + 回图）
python tools\local_chrome_panda_lite.py --account-id 1467 --sso-file .tmp\web-sso-canary-1467.json

# 批量入池（单 Chrome 顺序开票）
python tools\chrome_ticket_pool_minter.py --account-ids 1467,1470 --sso-file .tmp\web-sso-canary-1467.json
```

**验收（2026-07-23）**：

- Python 原型 1467 → HTTP 200，JPEG ~128KB。
- Go 票池 + `c07cc2e`：`pool_hit` 后 **HTTP 200**（asset 下载已对齐 Python）。
- **停止** `panda_chrometicket_image_acceptance.sh` 8 次循环长测；改用 [14](./14-chrome-ticket-lifecycle-experiments-2026-07-23.md) **S0 单次** + 分批实验。

**10 并发**：请求时 **0 Chrome**；灌池 **1 浏览器顺序**（池深提前灌够）。详见 [14](./14-chrome-ticket-lifecycle-experiments-2026-07-23.md) Batch M。


| 维度 | 冻结 Chrome 链（A） | Panda 生产（B） |
|------|----------------------|-----------------|
| 出票 | Playwright 真 Chrome | 本地 Python 签名器（无浏览器） |
| HTTP 客户端 | curl_cffi `chrome146` | Go + curl 传输 / egress lease |
| 代理 | 本机 Mihomo | udeal LA 单 sticky |
| 用途 | 逆向验证、本机 canary | grok2api 对外 `/v1/images/generations` |

生产问题（本文档正文）出在 **B 的阶段 1/2**，与 A 的「能否在本机 Chrome 出票」是不同运维面；排障时勿混淆。
