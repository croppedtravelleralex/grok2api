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

## 5. asset 403：已定位并修复 ✅

> **根因**：分阶段生图路径下，下载图片用的凭据**只有账号 ID、没有访问令牌**，
> 请求以未认证身份发出，被资产源站直接 403。修复见 §5.6。
>
> 本节结论曾两次被推翻（误判为「Go tls-client 指纹」「出口 IP 信誉」），
> 方法论教训见 §5.5。

### 5.0 确定的事实：不是 Cloudflare 反爬

2026-07-26 埋点上生产（commit `cdba9a1` / 镜像 `sha256:ae5df834…`）后抓到的真实响应：

```
resp_server        cloudflare
resp_cf_ray        a212884e7ed0e8ef-LAX
resp_cf_mitigated  (空)
resp_content_type  (空)
resp_body_snippet  (空)
req_cookie_names   sso,sso-rw
egress_node_id     110
```

`cf-mitigated` 为空、响应体为空、无 content-type —— **这是源站级 403**。
Cloudflare 真正拦截时会带 `cf-mitigated: challenge` 和整页 HTML。

**推论**：此前所有围绕 TLS 指纹 / header 顺序 / profile 版本 / IP 信誉的排查，
都在 Cloudflare 那一层做文章，而拦截根本不在那一层。

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

| 下载账号与资产所有者错配 | asset URL 路径含 owner user id；实测 250→`964f1ad2`、263→`caa5f655`、342→`7186f408` **全部匹配** |
| cookie jar 混入他账号 x-userid | 故意注入错误 `x-userid` + 伪造 `grok_device_id` | 仍 200 |
| CF cookie 缺失（用**从未成功下载过**的 URL 复测） | 仅 `sso` 与 `sso+CF warm` 都 200 |
| asset URL 新鲜度 / CDN 未传播 | 刚生成的 URL 立即下载 200（347KB） |
| **Go tls-client 指纹** | SSH 隧道让本地 Go 经 udeal 出口打同一 URL：**baseline 200** |
| TLS profile 版本 | Chrome_120/124/131/133/146 全 200 |
| header 顺序 / 伪头顺序 / 补全 Chrome 头 / 去 Origin | 9 个变体组合全 200 |
| 出口 IP 信誉 | 启用 4 个 webshare asset 节点跑生产：**仍 403**（同 IP 用 curl_cffi 是 200） |

累计 11 项，全部否定。

### 5.3 当前唯一未排除项

**生产 tls-client 是进程级长生命周期实例**（连续跑探针/对话/下载 24h+），
而所有探针都是每次新建客户端。差异只可能在：

1. 连接复用状态（HTTP/2 长连接）
2. 客户端内部 cookie jar 在显式 `Cookie` 头**之外追加**的内容

注意埋点里的 `req_cookie_names` 是**我们设置的头**，jar 追加发生在客户端内部、
日志看不到。定位方法：在 `egress/tlsclient.go` 的 `Do()` 里打出
`inner.GetCookies(target)` 与连接是否复用。

### 5.4 附带缺陷

`rewarmAssetDownloadCookie`（`chrometicket_download.go:148-152`）在 `deviceCookie == ""`
时直接返回，**无票路径完全跳过 CF 重warm**，6 次重试用同一 cookie。非根因，但让重试失去意义。

### 5.5 教训

多次误判（指纹 / IP / CF cookie / 连接复用 / HTTP 版本）有同一个模式：
**拿隔离探针的结果外推到生产**，而探针天然带着**正确的令牌**，恰好掩盖了
唯一的真实差异。

正确顺序是**先转储我方实际发出的请求字节**，再看对方为何拒绝。
只看响应（§5.0）仍不够——那只能判断「拒绝发生在哪一层」，判断不了
「我方发错了什么」。请求转储一上线就一击命中。

### 5.6 根因与修复

**证据**（生产埋点，commit `67bf4d7` 上线后）：

```
req_cookie_names   sso,sso-rw
req_cookie_len     13          ← 正常应约 320 字节（两个 152 字符 JWT）
```

13 字节正好是 `sso=; sso-rw=` —— **令牌为空**。

**根因**：`image.go` `downloadCredential` 在分阶段（staged）路径下返回
`account.Credential{ID: *id}`，只回填 ID，`EncryptedAccessToken` 是零值。
`downloadImage` 里 `Decrypt("")` 得空串，`BuildSSOCookie("")` 产出空 cookie，
请求以未认证身份发往 `assets.grok.com`。

这也解释了最关键的误导：日志里 `account_id` 是**对的**（ID 确实回填了），
所以「账号与资产所有者匹配」的核查通过，却仍 403；而外部任何客户端
（curl_cffi / wget / Go 探针）都能 200，因为它们带的是真令牌。

**修复**（commit `71148e7`）：

| 位置 | 改动 |
|------|------|
| `imagepipeline/scheduler.go` | `RunArtifacts` 增加 `SSCredential`；新增 `SetSSCredential` |
| `gateway/image_stage_provider.go` | 两处 `SetSSAccount(lease.Credential().ID)` → `SetSSCredential(lease.Credential())` |
| `web/image.go` | `downloadCredential` 优先返回 `SSCredential`，其次 ID 相同时回退 fallback |

**验收**：镜像 `sha256:78a6120d…`，连续生图 **3/3 → 200**（16/17/18s），
返回真实图片 URL；`web_lite_asset_download_failed` 归零。

---

## 6. 待办

| 项 | 说明 |
|----|------|
| **打出 tls-client jar cookie 与连接复用** | §5.3 唯一盲区，是当前生图唯一阻塞的定位手段 |
| `rewarmAssetDownloadCookie` 去掉 deviceCookie 前置 | 无票路径也应能重warm |
| `statsig_meta` 短路分支 | 有票时不应跳过真实 meta 刷新 |
| 票机制定位重估 | 上游已不要求票；保留与否取决于 asset 修好后的对照数据 |
| ~~二进制回归 GHCR~~ | **Done**：2026-07-26 走完整 git 链路发布，`.env` 固定 `sha256:ae5df834…` ← `cdba9a1` |

---

## 7. 部署链路（已按铁律执行一次完整闭环）

```
本地改测 → git commit → git push → Actions(Verify 全绿) → GHCR 双架构 + manifest merge
        → panda .env 固定 digest → docker compose pull && up → healthy
```

**Panda 上没有源码 git 仓库**，`/opt/grok2api/` 只有 `docker-compose.yml` + `config.yaml` + `data/`；
compose 用 `GROK2API_IMAGE` 按 digest 固定镜像，"部署"= 更新该 digest 后 pull && up。
镜像标签为分支名 `codex-panda-safe-completion`。`.env` 改动前备份 `.env.bak.20260726-softgate`。

规则见 `~/.claude/rules/common/panda-deploy.md`、`AutoRegister/AGENTS.md`、
`grokImage/.cursor/rules/panda-deploy.mdc`。

## 8. 本轮生产变更

新增 `tools/http_mint_probe.py`；推入并消费 1 张自铸票；账号 250/263/342 共消耗约 12 次
imagine 额度；二进制存档 `/opt/grok2api/binary-archive/grok2api-prod-20260726-preSoftGate`。

**事故（部署铁律建立前）**：`scp` 传二进制丢失可执行位，`docker cp` 后容器进入重启循环，
停机约 66 秒（08:48:06Z→08:49:12Z）。此类操作现已被铁律禁止。
