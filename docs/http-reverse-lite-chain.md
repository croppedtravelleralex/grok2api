# Frozen：Chrome 出票 + 纯 HTTP Chat / Lite

状态：**已冻结**（2026-07-18）  
正式入口：`tools/web_chrome_http_lite_frozen.py`  
实现库：`tools/_chrome_channel_chat_image.py`  
HTTP/助手：`tools/web_http_chat_image_canary.v1.py`  
两轮性能：`tools/_chrome_lite_bench.py`  
指针：`tools/web_http_chat_image_canary.py` → 正式入口  
账号池：`.tmp/web-sso-canary.json`（`panda_export_fresh_web_sso.py` / `panda_export_free_web_sso.py`）

## 2026-07-22 生产演进：业务链已无浏览器

本文件前半保留 2026-07-18 的 Chrome 短签冻结基线。当前生产主路径已经进一步演进为：browser-bridge 关闭，网关通过本地 signer `127.0.0.1:8788/sign` 为每次 REST 请求计算签名，再由纯 HTTP 客户端发起 `/rest/modes`、Chat 和 Lite 请求；正常生图请求期间不启动 Chrome。

当前 signer 仍依赖一次性真实 Chrome 捕获的 matched seed+HEX。静态抽取索引已从 `[31,16,43,8]` 漂移到 `[38,33,24,32]`，动态 challenge 虽能本地重算成功，但其 pair 曾被上游 code 7 拒绝。因此“业务链无浏览器”不等于“签名根完全不依赖浏览器证据”；sidecar 化、自动发现索引和真实 `/rest/modes` 强健康检查仍未完成。

生产已部署 10 个客户端 pipeline slots、100 FIFO queue、Expand=2、SSE AIMD `1→6`、Download=8。同账号 Lite lease 固定并发 1，账号在 SSE 结束后早释。10 槽只代表可同时进入流水线，不代表单住宅出口直接并发 10 条 SSE；当前真实阻塞是多个账号最终返回 `usage_limit_reached` 或 soft-stop，尚无 10/10 成功验收。

## 链路（固定）

```text
Mihomo 127.0.0.1:7897
        │
        ├─ B  Playwright channel=chrome（本机 Google Chrome，仅短签）
        │     注入 SSO → grok.com → 捕获 SPA /rest/* 的 x-statsig-id
        │     Cookie 罐含 cf_clearance
        │
        ├─ Q  （可选）curl_cffi POST /rest/rate-limits {modelName:fast|auto}
        │
        ├─ C  curl_cffi POST /rest/app-chat/conversations/new
        │     + x-statsig-id + PW Cookie；优先 /rest/modes 探针 chat_ok 的票
        │
        └─ D  同上 path，Drawing: … + enableImageGeneration → 落盘 .tmp/lite-images/
```

## 最小出票（独立单位）

**结论：行，但「最小」≠「无 Chrome」。** 今天仍要真实 Chromium/Chrome 过 CF + 跑 SPA 签名器；能做的是把出票从整链拆出，并砍掉非必要工作。

| 做法 | 能否降本 | 说明 |
|------|----------|------|
| 独立 `sign` 服务（只出票，不 Lite） | ✅ | HTTP Lite 与出票进程分离；Panda 只吃 HTTP |
| 拦 image/font/media/分析脚本 | ✅ | 缩小页面加载 |
| 早退：一有 `/rest/modes`+`cf_clearance` 就关浏览器 | ✅ | 跳过 turbopack 穷举、默认不做 UI 打字 |
| 常驻 1 个 headless（暖池）多账号轮流注入 SSO | ✅✅ | **最大降本**：均摊 Chrome 启动/内存，避免每请求冷启动 |
| 纯 curl / 无浏览器出票 | ❌ | 本机/外部 signer 未稳定；Turbopack 直签常失败 |

原型：`tools/_chrome_sign_minimal.py`（本机实测可 early_exit，耗时显著短于完整 canary 出票段）。

### 最小出票实测开销（本机）

| 指标 | 数值 | 说明 |
|------|------|------|
| 耗时 | **~9–10s** | early:`/rest/modes`+cf；完整链出票段曾 ~32–38s |
| Chrome RSS | 本机峰采样易被其它标签污染；**增量大致数百 MB 级** | Panda 历史单 bridge headless 约 **0.5–0.7GB** |
| 硬底 | **≥约 400–700MB + 短时近 1 核** | Blink/V8/CF 过盾，再砍步骤也去不掉进程 |
| 相对 panda | 2C/3.6G、`grok2api` 上限 512MiB | **仍顶不住常驻/每请求 Chrome**；时间变短 ≠ 内存变小到可塞进 panda |

再压空间：暖池（1 浏览器多号）、`chromium-headless-shell`（可能再瘦一截）、独立 ≥2C/4G worker。  
**不能**把 Chrome「拆成纯模块」去掉浏览器——签名器跑在 SPA/V8 里，CF 也要真浏览器。

## 出票需要什么配置

| 项 | 要求 |
|----|------|
| 账号 | 有效 Web **SSO**（panda `grok_web` 解密导出） |
| 浏览器 | 本机安装 **Google Chrome**；Playwright `channel="chrome"` |
| 代理 | Mihomo **`127.0.0.1:7897`**（PW 与 curl_cffi 同出口） |
| Python | 含 `playwright`、`curl_cffi`（本机用 gptimage `.venv`） |
| TLS 伪装 | `impersonate=chrome146`（库最高；本机 Chrome 150） |
| UA | `Chrome/150.0.0.0`（与系统 Chrome 大版本对齐） |
| 环境 | 清掉坏的 `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` 等，否则 curl TLS 抖 |

## 票（x-statsig-id）怎么用、能用多久

> **2026-07-23 更新**：Panda 票池实验链已验收。长效资产为 **`statsig_meta`**（`twitter:site-verification`），短效 `statsig` 由 Panda `grok-signer` 按请求刷新。详见 [13-chrome-ticket-pool-panda-api-2026-07-23.md](./13-chrome-ticket-pool-panda-api-2026-07-23.md)。

| 问题 | 结论 |
|------|------|
| 出一次票只能打一个生图吗？ | **不必然。** 成功轮常见：同一张 `/rest/modes` 票 → PONG 探针 + chat + **一次** Lite，间隔约十几秒内。 |
| 每个生图请求都要重新出票吗？ | **票池模式**：存 `statsig_meta`（6～24h），请求时 Panda `grok-signer` 现签。单次链仍建议 **票即用**。 |
| 票能用多久？ | **`statsig` ~45s**；**`statsig_meta` 小时级**。勿缓存裸 `statsig` 做池。 |
| 和 Cookie 的关系 | Panda 侧 egress warm 取 CF；票里只存 `grok_device_id` 等设备 Cookie。 |
| 路径敏感？ | **是。** Panda 用 `grok-signer` 对 `conversations/new` 现签，不依赖 Chrome 捕获的 path 错票。 |
| 失败表现 | `systemErrCode:1010` → 账号 **imagine 额度为 0**（如 1468），非链路故障。 |

**推荐生产用法（票池）：**  
本机 Chrome 批量捕获 meta → Panda SQLite 票池 → API pop → signer 现签 → Lite → 回图。详见 doc 13。

## 关键常量

| 项 | 值 |
|----|-----|
| 出票 | Playwright `channel="chrome"` |
| TLS | `curl_cffi` **`chrome146`** |
| UA | `Chrome/150.0.0.0` Windows |
| 票偏好 | `/rest/modes` → `chat_ok` |
| 代理 | `http://127.0.0.1:7897` |
| 额度 | `POST /rest/rate-limits` |

## 额度可读性

| 字段 | 可读？ | 说明 |
|------|--------|------|
| `remainingQueries` / `totalQueries` | 是 | `fast`/`auto`/… |
| `windowSizeSeconds` | 是 | `ResetAt ≈ now+window`（推算） |
| Imagine 专用次数 | **是** | `GET /rest/usage/free-usage-gates` → `imagine.allowance/remaining`，字段可能是字符串或数值 |
| Imagine 重置时间 | 否 | 上游未返回窗口秒数或绝对 ResetAt，不得伪造“每日几点刷新” |
| Imagine `0/0` | 上限未知 | 历史 Lite 成功账号也可能返回 `0/0`；不能据此判定耗尽。剩余次数须靠闸门正数、Lite 探针或近期成功证据；见 [23-lite-vs-imagine-quota-2026-07-27.md](./23-lite-vs-imagine-quota-2026-07-27.md) |

## 性能开销（实测口径）

| 指标 | 含义 |
|------|------|
| Chrome ~0.5–2GB 峰 | **出票短签**；非 Lite HTTP 常驻。Panda 历史 bridge ~0.5–0.7GB 即顶门槛 |
| Python ~150–270MB | canary + Playwright 驱动 |
| 带宽/张 | 生产样本 mean **~170KB** JPEG（p50 ~153KB）；分辨率 **784×1168** 约 1K |
| CPU | 出票占绝大部分；Lite 阶段本机很低 |

**Panda：** 2C/3.6G，不适合每请求 Chrome；适合只跑纯 HTTP Lite。

## 边界

- Lite **无** `aspect_ratio`；文案写 16:9 **不保证**像素（常约 1168×784）。
- **16:9 / 2K** → Imagine WS（`_imagine_once.py`），不在本冻结。
- 禁止人工点网页；失败即停。

## 复跑

```powershell
$py='D:\SelfMadeTool\AutoRegister\gptimage\.venv\Scripts\python.exe'
cd D:\SelfMadeTool\AutoRegister\grokImage
# 导出 free/basic 号
Get-Content tools\panda_export_free_web_sso.py -Raw |
  ssh panda "cat > /tmp/panda_export_free_web_sso.py && python3 /tmp/panda_export_free_web_sso.py 4 <exclude>" |
  Set-Content -Encoding utf8 .tmp\web-sso-canary.json
# 正式冻结入口
& $py tools\web_chrome_http_lite_frozen.py --account-id 400 --prompt "a realistic tuxedo cat, black and white"
# 或指针
& $py tools\web_http_chat_image_canary.py --account-id 400
```

## Imagine 是什么

Grok Web **Pro 生图**（`/imagine` + `wss://…/ws/imagine/listen`），可控 `aspect_ratio` / `1k|2k`。本冻结是 **Lite Drawing REST**，不是 Imagine。

## 旧路径（已取代）

Playwright bundled Chromium + `chrome131`：仅对照；正式以 **Chrome channel + chrome146** 为准。

## Panda 生产排障（2026-07-23）

生产已走零浏览器签名 + udeal，与上文冻结 Chrome 链不同。两阶段失败（soft_stop / asset 403）、探针与链路对照见 **[12-web-lite-two-stage-failure-asset-403-2026-07-23.md](./12-web-lite-two-stage-failure-asset-403-2026-07-23.md)**。

## 本机 Chrome 开票 → Panda 用票（2026-07-23，已验收）

编排：`tools/local_chrome_panda_lite.py` → SSH → `tools/panda_lite_with_ticket.py`（Panda `grok-signer` 现签 + Lite + 回图）。

票池与 API 设计见 **[13-chrome-ticket-pool-panda-api-2026-07-23.md](./13-chrome-ticket-pool-panda-api-2026-07-23.md)**。
