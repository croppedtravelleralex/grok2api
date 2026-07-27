# grokImage 当前状态主档

## 最后更新时间

- 日期：2026-07-27
- 维护目的：**住宅 asset cookie 修复 + 20 并发 profile 推进**；asset 亲和根因为 cookie IP 绑定非哈希环。见 [22](./22-proxies-concurrency-and-bottlenecks-2026-07-27.md) §7、[21](./21-ticket-obsolescence-and-asset403-tls-2026-07-26.md)。

## 整体状态摘要

- **（2026-07-26 推翻的四条旧认知）**见 [21](./21-ticket-obsolescence-and-asset403-tls-2026-07-26.md)：
  1. 开票**不需要**本机 Chrome —— 一次带 SSO 的首页 GET 即可拿到 `grok_device_id` + `x-userid`（`tools/http_mint_probe.py`，全链路生产 API 200）。
  2. 票里的 `statsig_meta` 是**死数据** —— signer 只用启动锁定的 pair，忽略传入 metaContent。
  3. `grok-imagine-image` **不再必须有票** —— 无票裸跑上游 3/3 出图；硬门禁已改软（票池空不再 503）。
  4. asset 403 **与票无关**，也不是 Cloudflare 反爬 —— 根因是下载图片时用的凭据**只有账号 ID、没有访问令牌**，请求以未认证身份发出被源站 403。**已修复**。
- **asset 403 已解决（2026-07-26）**：`downloadCredential` 在分阶段路径下只回填 ID，`EncryptedAccessToken` 为空 → cookie 变成 13 字节的 `sso=; sso-rw=`。修复为在 `RunArtifacts.SSCredential` 里保存出图阶段的完整凭据。沿途排除 17 项假设，全部非根因。详见 [21](./21-ticket-obsolescence-and-asset403-tls-2026-07-26.md) §5.6。
- **（2026-07-27 住宅 asset）**：20 条 Webshare 住宅已注册为 `grok_web_asset`（`wsres-asset-001~020`）。禁 udeal-111 后全挂的根因是 **grok.com 预热 CF cookie 被发到住宅 IP**；修复为无 `leaseCF` 时仅 SSO+device identity，`rewarm` 改 `ScopeWebAsset`。BE-024 只读观测：`ticketReadyIds` / `slotRegistryIds` 已入 Admin 快照。
- **（2026-07-27 并发目标）**：udeal 单线 30 并发认证 30/30 200；Panda 下行实测 108–180 Mbps。目标 profile：`webConcurrency=20`、`promptSlots/sseSlots=20`、`queue=200`（`tools/panda_apply_web20_profile.py`）。**尚未完成 10/20 生产验收**。
- **pin 自愈已上线（2026-07-26）**：新增后台任务 `image_dispatch_pin_sync`（启动 45s 首跑、此后每 5min），把 grok-imagine-image 的 pin 与四池 dispatch 自动对齐；同时移除 `imageDispatchPinTargetIDs` 的按票收窄（票只影响选号排序，不再决定可选集合）。上线即 `pinned=33 added=30`，`dispatchImageLen` 3 → **33**。此前 pin 只有手工 HTTP 入口、长期冻结在 3 个号，是「当前没有可用的上游账号」503 的直接成因。
- **生产二进制已可溯源（2026-07-26）**：走完整 git 链路发布，`.env` 固定 `sha256:ae5df834…` ← commit `cdba9a1`，`docker cp` 孤儿二进制已被取代。同批回流了一处**此前只存在于生产二进制**的修复：`settings/service.go` 整体替换 `base.Routing` 时会把 `DisableCooldown` 静默重置为 false。
- **部署铁律（强制）**：禁止在 Panda 编译任何项目；部署只走 `git push → Actions → GHCR → compose pull && up`；禁止 `scp` / `docker cp` 部署。Panda 上**无源码 git 仓库**。规则见 `~/.claude/rules/common/panda-deploy.md`。

- 后端为 Go 网关，前端为 React/Vite 管理端，支持 Grok Build、Web、Console 三个账号池。
- Panda 为低资源生产机：**禁止在其上编译/构建**；标准链为本地改测 → GitHub 上传（Actions/GHCR）→ Panda 仅 `pull` 运行。**禁止**用 tar/scp 传大包到 Panda 再 `go build`。
- **Build（2026-07-22）**：四池主路径已上线；生产快照约 `dispatch≈211 / normal=2 / verification=0 / delete=0`。已做/未做全量盘点与 FP-* 待办见 [08-build-four-pool-dual-probe-todos-2026-07-22.md](./08-build-four-pool-dual-probe-todos-2026-07-22.md)。
- **Web（2026-07-23）**：Chrome 票池 + asset 下载修复已上生产。镜像 `sha256:49f23f31…7a841`（`c07cc2e`）；1467 `pool_hit` 后生图 **200**。持续灌票与生命周期实验见 [14](./14-chrome-ticket-lifecycle-experiments-2026-07-23.md)。
- **票池实现**：**Go**（`chrometicket` + `chrome_tickets` 表）。本机灌池/实验脚本为 **Python PoC** → 验收后 **Rust** 工具链（见 [plan.md](./plan.md) 语言分层）。
- **实现纪律**：**Python 只设计原型**；跑通后 **Rust** 实现本机/运维 CLI；**Go** 负责 Panda 服务端（调度/票池存储/egress）。
- **后续主线（2026-07-26）**：**BE-024 TicketReady 销票槽位**（票池逻辑并入号池调度视图）。架构 [20](./20-ticket-ready-slot-dispatch-merge-2026-07-25.md)；计划 [plan.md](./plan.md)。BE-019/018/020/021/023 已落地；plan §4 **生图 ok≥1** 仍未过。
- **票池 × dispatch 漂移（2026-07-25）**：available ~19 票，runtime/pin 可缩至 1～3；四套集合无单一真相 — [20](./20-ticket-ready-slot-dispatch-merge-2026-07-25.md)。
- **routing.disableCooldown（2026-07-25）**：默认 `true`；Panda 曾 `disable-cooldown-v9` 二进制；**须 GHCR 正式化**。
- **探针工具（部分未 commit）**：`chrome_ticket_pool_probe.py --remediate`、`chrome_ticket_probe_rs/`、`panda_unified_pool_snapshot.py` — [20](./20-ticket-ready-slot-dispatch-merge-2026-07-25.md) §2.2。
- **票实验门禁**：R-delay/S 部分完成；**V-conc / mint_fast / S-3h 验收冻结**，直至 plan §4 门禁通过。
- **票实验（2026-07-23）**：S0 / D-1m/3m/5m 跨网延迟消费均 **200+pool_hit**；CF403/IP 认知重排见 [15](./15-chrome-ticket-cf403-ip-reframe-2026-07-23.md)。
- **Admin 认证（2026-07-23 23:22）**：Panda 曾出现 secrets / DB / import `.tmp` **三套密码源不同步** → 实验脚本 401、import 仍 200；已重置并三源同步，login 200。工具：`_panda_reset_admin_password.py`、`_panda_admin_token.py`。
- **Web（2026-07-22）**：Lite 主路径已切纯 HTTP——关 browser-bridge + 本地 signer + 单住宅出口。
- **Imagine 次数读取（本地已实现、生产未部署）**：`GET /rest/usage/free-usage-gates` 可返回 `imagine.allowance/remaining`；上游不返回窗口长度或绝对重置时间。实测多个账号（含历史 Lite 成功号）均可能返回 `0/0`，因此 `0/0` 定义为“免费闸门不适用或上限未知”，只有 `total>0 && remaining=0` 才能判定额度耗尽。
- **账号×模型独立状态（本地已实现、生产未部署）**：持久化 `unknown / quota_available / available / soft_stop / quota_exhausted / auth_failed / signature_failed`；额度窗口与真实请求结果分开保存。Accounts API/页面会同时展示 Imagine 次数和模型状态。
- 本轮已做、未做、生产门禁与下一步完成定义统一见 [09-imagine-quota-model-state-and-10-concurrency-todos-2026-07-22.md](./09-imagine-quota-model-state-and-10-concurrency-todos-2026-07-22.md)。
- Web 调度当前 **20 个账号**进池：`641,642,644,646,647,649,650,652,654,656` + `659,661,663,667,669,671,673,674,675,677`；文生图开、图生图/视频关；NewAPI 渠道 `#105` 已启用。
- NewAPI `#105` BaseURL：`http://grok2api:8000`（容器直连，避开 CF；需 `new-api` 加入 `grok2api_default` 网络）。
- **NewAPI 文生图已验收**：同机 e2e 曾 **200**（约 7–11s），媒体 URL 落在 `https://grokimage.relai.asia/v1/media/images/...`（token 须 `group=grok`、DB key 48 位无连字符、无 `sk-` 前缀）；池薄时仍会 `429 usage_limit`。
- **10 并发状态**：10 槽/100 queue、Expand 2、SSE AIMD 1→6、Download 8 已部署；这表示 10 个客户端请求可同时进入流水线，不表示单出口同时发 10 条 SSE。最终单请求仍连续 `usage_limit_reached`，所以 4/10 生产档未运行，10/10 成功尚未验收。
- NewAPI 图片名称仍为 `grok-imagine-image` / `quality` / `edit`；edit/video 渠道保持 disabled。
- **Chrome 票池（2026-07-23）**：Go 票池 + 本机 Chrome minter + signer 现签；**有票路径** E2E 200。运维：**本机持续出票**（池深≥3）；实验改 **分批短跑**（[14](./14-chrome-ticket-lifecycle-experiments-2026-07-23.md)），停止 30～60min 单次长测。
- **Chrome 票池可视化（2026-07-24）**：账号页 Grok Web 下新增 **Chrome 票池** 面板（`GET /chrome-tickets/stats`）；展示 available/consumed/expired 与按账号分布。

## 已完成功能

### 基础架构

- SQLite/PostgreSQL、内存/Redis 运行时、账号池、模型路由、请求审计和代理出口。
- Panda 域名入口为 `grokimage.relai.asia`，生产服务由容器运行。
- Web 与 Web Asset 使用分离的出口作用域与可配全局闸门（`webConcurrency` / `assetConcurrency` / `expandConcurrency`）；单 udeal 基线 2 / 8 / 2。Lite 文生图另有 `ImagePipelineScheduler`（10 槽 + 显式 FIFO/aging + SSE AIMD），管理端「时序图」展示槽位 owner、阶段等待和准入队列。

### 核心业务能力

- 支持 Responses、Chat Completions、Anthropic Messages、图片生成/编辑和异步视频接口。
- Chat/Messages 对外响应模型固定为公开模型名，不再泄露 `grok-4.5-build-free` 这类上游内部型号；账号的真实观测型号仍保留在后端。
- Messages 流式尾事件会返回完整输入、输出和缓存命中 Token；Claude Code 的 `cache_control` 稳定前缀会派生粘滞键。
- Build `permission-denied` / `access_denied` / OAuth `invalid_grant`：进**删除池**，由维护探针物理删除（`GROK2API_BUILD_SAFE_PURGE_APPLY` 默认 true）。
- **Build 四池**：验证（无 `observed_model`）→ 调度（有额度）/ 普通（额度用尽或短冷却）→ 删除；路由 `ListRoutingCandidates` 仅已验证号；Selector 走 `DispatchIndex` 有序试租约。
- **双探针**：调度探针巡检调度池（最久未探堆）；维护探针 DRR 吃验证/普通/删除（5:3:2，验证空则 7:3）；合计并发 ≤2。
- 启动时一次性把旧 `reauthRequired` / `retired:` backlog 标为 `deletable:` 并重建四池索引。
- 外挂 `grok2api-account-pool-probe` **跳过 Build**，只处理 Web/Console，避免干扰四池。
- Anthropic Messages：上游 `cached_tokens` 按 Anthropic 语义拆成 `input_tokens` + `cache_read_input_tokens`；Claude Code 的 `cache_control` 派生键会写入上游 `prompt_cache_key`，便于返回缓存命中。
- Accounts 页 Build 探针面板：四池计数、双探针模式/结果、删除执行开关。
- 管理接口 `GET/PATCH /api/admin/v1/accounts/build-probe`；池数量从数据库实时汇总。
- Web→Build 转换在已关联 Build 账号处于失效态时会更新原账号凭据，而不是错误地跳过。
- 图片本地归档已记录请求 ID、模型、请求分辨率、实际宽高、生成耗时和精确到秒的时间。
- 图片管理已支持本地日期筛选、按日期分组、按日期删除和一键删除全部。

### 维护性与工程能力

- 后端 `go test ./...` 与前端 `pnpm lint`/`pnpm build` 以本轮变更验收为准。
- Panda 低资源操作规则：仅 `docker compose pull && up`，禁止主机编译。

## 账号池诊断事实

- 历史（2026-07-16）七池/恢复/软退役模型已废弃；上线后以四池 API 快照为准观察 `delete` 下降与 `dispatch` 稳定。
- `invalid_grant` / `access_denied` 不再进入长期恢复队列；≤1～2 个维护周期内应物理删除。
- Web 路径事实见 [07](./07-udeal-zero-browser-ops-2026-07-21.md)。
- Web Imagine 的三类失败必须分开：流水线满为本地 429；`usage_limit_reached` 为上游真实 429；SSE `isSoftStop=true` 常对外表现为 502。单请求也会 soft-stop，双账号并发不是必要触发条件。
- 近期成功账号优先曾导致两条并发 Lite 请求拿到同一账号；已把同账号 Lite 并发固定为 1。复验中并发请求使用不同账号，不再出现 `too many requests in progress`。
- Imagine 图池不再借用聊天 `fast` 额度：独立使用 `imagine` 窗口、模型状态和模型级 block。真实成功优先，已知正额度待探测其次，`0/0`/未同步保持未知但可路由；明确耗尽、认证失败、签名失败和仍在冷却的 soft-stop 不进入图池。聊天池仍只看 `auto/fast`。

## 进行中事项

- **P0（新）**：**BE-024** TicketReady 销票槽位 + 双池调度语义合并。见 [20](./20-ticket-ready-slot-dispatch-merge-2026-07-25.md)。
- **P0**：**BE-023** Web Image 四池 — **Done**（`web_pool_probe.go`）；与 BE-024 衔接：dispatch 准入仍须与票槽对齐。
- **Done（2026-07-24）**：`BE-018` egress 流量、`BE-019` pin∩dispatch、`BE-020` selection_reason、`BE-021` 选号偏好有票。
- **冻结**：Chrome 票压测（S-3h/V-conc/mint_fast）至门禁 **生图 ok≥1** + 四池落地。
- **P0（2026-07-21）**：Build 四池双探针观察。
- 历史待办 [06](./06-open-todos-2026-07-16.md)：探活可视化已完成。

## 已知阻塞与风险

- **调度池与票池脱钩（P0）**：票按号存储，dispatch 出入不看票 → 孤儿票与 runtime 压扁。→ **BE-024**。见 [20](./20-ticket-ready-slot-dispatch-merge-2026-07-25.md)。
- **调度池准入过宽（P1，四池后缓解）**：`soft_stop`/`quota_exhausted` 号进 dispatch 问题已由 BE-023 收窄；仍须与 TicketReady 对齐。
- **号池索引（BE-019 已修）**：pin∩dispatch 已对齐；Phase B 验证 `pinNotInDispatch=[]`。展示池与 dispatch 仍须四池后统一投影。
- **生图 E2E 低**：当前瓶颈是 **账号额度/soft_stop**，不是票失效；开票解决 asset 403，不解决 429。见 [17](./17-web-four-pool-and-imaging-success-rates-2026-07-24.md)。
- **egress 流量**：BE-018 已插桩；smoke 脚本审计 API 路径待修。
- **10 并发生产未验收**：冻结至双池门禁后恢复。
- **出口与票分层**：票可跨 IP；消费须可用 egress（udeal）。Webshare 不能作 `grok_web`（CF challenge）。udeal 冷却 → 503，非票失效。
- **独占 pin ∩ 内存图池**：须 pin 在 image dispatch 内，或重启重建索引。
- signer 静态索引已从 `[31,16,43,8]` 漂移到 `[38,33,24,32]`；更严重的是 8 分钟动态 challenge 重算会生成服务端不接受的 pair。当前临时使用一次性 Chrome 捕获的 matched seed+HEX，并把刷新窗口放宽到 24h。signer 仍未镜像化，也没有“真实签名被上游接受”的强健康检查。
- Webshare **不可**作 `grok_web`（CF Managed Challenge）；可仅试验 `grok_web_asset` CDN 下图。
- 单住宅出口：上行 ~1.5 Mbps / 下行 ~15 Mbps；Lite 出图约 **170KB JPEG / 784×1168**。当前保持 `webConcurrency=2`、`assetConcurrency=8`、`expandConcurrency=2`、`mediaConcurrency=1`，SSE target 从 1 上探；账号接受率恢复前不得把 web/SSE 下限抬高。
- Lite 文生图：准入排队 → 扩写池 → SSE AIMD → 下图池；账号 lease 在 SSE 后早释；`/image-timeline` 甘特图。
- 图生图暂关；开启前需额度与冷却策略，且同口上传应串行。

## 下一步 3-5 项

1. 拍板 **BE-024**：SlotRegistry 大小 N、出池时清票 vs 阻塞出池。
2. 实施 P0：TicketReady 探针指标 + JIT 只灌槽位 + 孤儿清扫 + pin sync 改 SlotRegistry 驱动。
3. `disableCooldown` 与探针工具 **commit + GHCR**（禁止长期依赖 Panda 二进制替换）。
4. 通过 plan §4 门禁后恢复 S-3h / V-conc / mint_fast。
5. **BE-022** Rust 工具链（pool-ops / minter / experiment）PoC 契约冻结后推进。

## 与 README 或旧文档的不一致处

- README / 旧 current-state 仍写「Web Cloudflare 不可用 / 强制 browser-bridge」——**已过时**；以本文件与 [07-udeal-zero-browser-ops-2026-07-21.md](./07-udeal-zero-browser-ops-2026-07-21.md) 为准。
- README 描述 Grok Web SSO 不可自动续期；本项目可以保活、同步和重新导入，但不能从已失效 SSO 凭空生成新凭据。
- README 未详细说明 Panda 的生产资源限制，Panda 操作以本维护文档和全局 `panda-remote-ops` 规则为准。
- 旧文「生产/恢复/隔离/退役七池」已过时；以本文件四池 + 双探针为准。
