# 票池失效性验证 + asset403 定位到 Go TLS 指纹（2026-07-26）

> **状态**：生产实测结论档。三个长期假设被推翻。
> **关联**：[20-ticket-ready-slot-dispatch-merge](./20-ticket-ready-slot-dispatch-merge-2026-07-25.md)、[17-web-four-pool](./17-web-four-pool-and-imaging-success-rates-2026-07-24.md)、[18-pure-http-statsig-meta-poc](./18-pure-http-statsig-meta-poc.md)、[plan.md](./plan.md)

## 结论摘要

| 项 | 旧认知 | 实测结论 |
|----|--------|----------|
| 开票必须用本机 Chrome | 是 | **否**。一次带 SSO 的首页 GET（curl_cffi）即可拿到全部票材料 |
| 票里的 `statsig_meta` 参与签名 | 是 | **否**。signer 忽略该字段，是死数据 |
| `grok-imagine-image` 必须有票 | 是 | **否**。无票裸跑上游 3/3 出图成功 |
| 票解决 asset 403 | 是 | **否**。asset 403 是 Go 客户端 TLS 指纹问题 |

---

## 1. 票的真实构成

`chrome_tickets` 表字段：`statsig_meta` / `device_cookie` / `user_agent` / `sign_source`。

### 1.1 `statsig_meta` 是死数据

- 请求时经 `statsig.go:375` 作为 `metaOverride` 传给 signer
- signer（`signer/app.py:159`）只用启动时锁定的 pair：`sign_request(method, target_path)`，**metaContent 仅为协议兼容**
- 实测：190 张票里有 **179 个互不相同的 meta**，而 signer 锁定的 meta 与**任何一张都不同**

副作用（未修）：票存在时 `Sign()` 走 `metaOverride` 短路分支，跳过首页 meta 抓取，导致真实 meta 缓存在有票期间不刷新。

### 1.2 `device_cookie` 只有两项有效

实测 6 张票的 cookie 名分布：`grok_device_id`(6/6)、`x-userid`(6/6)、`mp_*_mixpanel`(6/6)、`i18nextLng`(6/6)、`OptanonConsent`(5/6)、`__stripe_mid/sid`(5/6)。后四类是分析与同意 cookie，搭便车。

| cookie | 性质 | 纯 HTTP 可得 |
|--------|------|--------------|
| `grok_device_id` | 每会话新生成（250 号 45 票 → 37 个不同值） | ✅ 首页 Set-Cookie |
| `x-userid` | 账号内恒定（45 票 → 1 个值），非 SSO 派生（≠ JWT `session_id`） | ✅ 首页 Set-Cookie（须带 SSO） |

---

## 2. 纯 HTTP 铸票已验证可行

工具：`tools/http_mint_probe.py`（本轮新增，复用 `panda_lite_with_ticket.py` 的 SSO/出口解密）。

```json
{"account_id":250,"http":200,"elapsed_s":2.08,
 "jar_names":["__cf_bm","grok_device_id","x-userid"],
 "has_grok_device_id":true,"has_x_userid":true,"meta_len":64}
```

交叉验证：自铸 `x-userid` 与该账号历史 Chrome 票**逐字节相同**；`grok_device_id` 为新值。

全链路：推入生产票池 → `POST /v1/images/generations` → **http=200，14s**，日志 `chrome_ticket_pool_hit sign_source="http_mint_poc"`，审计 `250|200|13838ms`。

> 前置阻塞「生产侧没有自己的铸票能力」**不成立**。BE-024 的 JIT 按槽位铸票可行。

---

## 3. 无票生图可行（手动 HTTP 链路 3/3）

`panda_lite_with_ticket.py` 传 `cookie:""`，账号 250：

| 轮次 | chat | asset | 产物 |
|------|------|-------|------|
| #1 | 200（5 chunks） | 200 | 147,574B JPEG |
| #2 | 200（5 chunks） | 200 | 180,729B JPEG |
| #3 | 200（5 chunks） | 200 | 237,457B JPEG |

`anti_bot=false`，无 403。

---

## 4. 硬门禁改软（已上生产）

改前：票池为空 → `filterChromeTicketCandidates` 返回 `SelectionNoChromeTickets` → **503 `chrome_ticket_unavailable`**，上游根本没被请求。即在上游可出图的情况下自我阻断。

改动（`selector.go`）：

- `filterChromeTicketCandidates` 不再返回 error；无持票候选时**回退原候选集**，保留「优先选有票账号」
- 移除 `len(normalCandidates)==0` 分支里会误报的 `SelectionNoChromeTickets` 归因
- 删除随之无用的 `chromeTicketsEnforced()`
- 测试改为 `TestSelectorPrefersChromeTicketHoldersAndFallsBackWhenPoolEmpty`

同批修复（`settings/service.go:306`）：`base.Routing` 整体替换时漏掉 `DisableCooldown`，会把它静默重置为 `false`。持久化设置无该项，改为保留既有值。**该修复原本只存在于生产二进制、未回流仓库。**

验证：票池 available=0 时生图**不再 503**，请求进入上游并成功出图，卡在下载（见 §5）。

---

## 5. asset 403 定位：Go tls-client 指纹

### 5.1 现象

```
web_lite_asset_download_failed  scope=grok_web_asset  403
web_lite_asset_download_fallback → grok_web
web_lite_asset_download_failed  scope=grok_web  403 ×5
image_upstream_failed  "下载图片返回 403"  → 502
```

3 次请求、2 个账号（263/342）、不同 URL，全部同样失败。**图在上游已生成，只是取不回来。**

### 5.2 逐项排除

| 假设 | 排除依据 |
|------|----------|
| 出口节点不同 IP | node 110 与 111 **同一 IP** `70.39.164.200:30000` |
| 缺 CF cookie | A/B 实测：仅 `sso` 与 `sso+CF warm` **都是 200** |
| 缺 device cookie（无票） | 无票 curl_cffi 下载同样 200 |
| 请求头缺失 | `applyAssetDownloadHeaders` 已设 Accept/UA/Origin/Referer/Cookie，与 harness 一致 |
| Cookie 格式不同 | `BuildSSOCookie` 与 harness `merge_cookie` 都产出 `sso=X; sso-rw=X` |
| 账号或额度 | 换账号复现 |

### 5.3 决定性证据

取 Go 刚 403 六次的**同一 URL**，用 curl_cffi 走**同一代理、同一 cookie、同一 UA、同一请求头**：

```
curl_cffi 下载 Go 刚 403 的同一 URL: http=200 bytes=205256 jpeg=True
```

两侧都声称 Chrome 146：Go 用 `bogdanfinn/tls-client` `profiles.Chrome_146`（`egress/tlsclient.go:35`），Python 用 curl_cffi `impersonate="chrome146"`。**实现不同，`assets.grok.com` 的反爬只放行后者。**

### 5.4 附带缺陷

`rewarmAssetDownloadCookie`（`chrometicket_download.go:148-152`）在 `deviceCookie == ""` 时直接返回，**无票路径完全跳过 CF 重warm**，6 次重试用同一 cookie。虽非本次 403 根因，但让重试失去意义。

---

## 6. 待办

| 项 | 说明 |
|----|------|
| asset 下载换客户端 | 让 Go 侧 asset 下载走 curl_cffi 指纹（sidecar 或换库），是当前生图唯一阻塞 |
| `rewarmAssetDownloadCookie` 去掉 deviceCookie 前置 | 无票路径也应能重warm |
| `statsig_meta` 短路分支 | 有票时不应跳过真实 meta 刷新 |
| 票机制定位重估 | 上游已不要求票；保留与否取决于 asset 客户端修复后的对照数据 |
| 二进制回归 GHCR | 生产长期靠 `docker cp`，无法溯源 |

---

## 7. 本轮生产变更

新增 `tools/http_mint_probe.py`；二进制存档 `/opt/grok2api/binary-archive/grok2api-prod-20260726-preSoftGate`（替换前原件，可回滚）；推入并消费 1 张自铸票；账号 250 消耗约 5 次 imagine 额度。

**事故**：`scp` 传二进制丢失可执行位，`docker cp` 后容器进入重启循环，停机约 66 秒（08:48:06Z→08:49:12Z）。后续 `docker cp` 前须 `chmod 755`。
