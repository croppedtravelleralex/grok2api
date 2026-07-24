# grokImage 当前状态主档

## 最后更新时间

- 日期：2026-07-24（晚间）
- 维护目的：**暂停生图压测**；BE-019/018/020/021 已落地；下一步 **Web 四池准入**（BE-023）。见 [plan.md](./plan.md)、[17](./17-web-four-pool-and-imaging-success-rates-2026-07-24.md)。

## 整体状态摘要

- 后端为 Go 网关，前端为 React/Vite 管理端，支持 Grok Build、Web、Console 三个账号池。
- Panda 为低资源生产机：**禁止在其上编译/构建**；标准链为本地改测 → GitHub 上传（Actions/GHCR）→ Panda 仅 `pull` 运行。**禁止**用 tar/scp 传大包到 Panda 再 `go build`。
- **Build（2026-07-22）**：四池主路径已上线；生产快照约 `dispatch≈211 / normal=2 / verification=0 / delete=0`。已做/未做全量盘点与 FP-* 待办见 [08-build-four-pool-dual-probe-todos-2026-07-22.md](./08-build-four-pool-dual-probe-todos-2026-07-22.md)。
- **Web（2026-07-23）**：Chrome 票池 + asset 下载修复已上生产。镜像 `sha256:49f23f31…7a841`（`c07cc2e`）；1467 `pool_hit` 后生图 **200**。持续灌票与生命周期实验见 [14](./14-chrome-ticket-lifecycle-experiments-2026-07-23.md)。
- **票池实现**：**Go**（`chrometicket` + `chrome_tickets` 表）。本机灌池/实验脚本为 **Python PoC** → 验收后 **Rust** 工具链（见 [plan.md](./plan.md) 语言分层）。
- **实现纪律**：**Python 只设计原型**；跑通后 **Rust** 实现本机/运维 CLI；**Go** 负责 Panda 服务端（调度/票池存储/egress）。
- **后续主线（2026-07-24）**：**BE-019/018/020/021 已提交**（`96b664d`）；Phase B smoke：**503=0** 但 **生图 0/10**（429/soft_stop）。下一步 **Web 四池**（仅真实额度+真实可用进调度）。计划见 [plan.md](./plan.md)；成功率对照见 [17](./17-web-four-pool-and-imaging-success-rates-2026-07-24.md)。
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

- **P0（新）**：**BE-023** Web Image 四池（对齐 Build；dispatch 门槛 = `candidateImagineQuotaAdmissible`）。见 [17](./17-web-four-pool-and-imaging-success-rates-2026-07-24.md)。
- **Done（2026-07-24）**：`BE-018` egress 流量、`BE-019` pin∩dispatch、`BE-020` selection_reason、`BE-021` 选号偏好有票。
- **冻结**：Chrome 票压测（S-3h/V-conc/mint_fast）至门禁 **生图 ok≥1** + 四池落地。
- **P0（2026-07-21）**：Build 四池双探针观察。
- 历史待办 [06](./06-open-todos-2026-07-16.md)：探活可视化已完成。

## 已知阻塞与风险

- **调度池准入过宽（P0）**：`soft_stop`/`quota_exhausted` 号仍可进 dispatch（尤其 pin 脚本洗 `available`）；生图 429/502 非 503。→ **BE-023 四池**。
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

1. 实施 **BE-023**：Web Image 四池 + `WebPoolAt` 重写；pin 脚本禁止洗状态。
2. 修 `panda_gate_smoke_phase_b.py`：`gate_passed` 要求 `ok≥1`；egress 审计路径对齐 Admin API。
3. 重跑 Phase B smoke，确认 dispatch 仅含真实可用号。
4. 通过 plan §4 全部门禁后，恢复 S-3h / V-conc / mint_fast。
5. **BE-022** Rust 工具链（pool-ops / minter / experiment）PoC 契约冻结后推进。

## 与 README 或旧文档的不一致处

- README / 旧 current-state 仍写「Web Cloudflare 不可用 / 强制 browser-bridge」——**已过时**；以本文件与 [07-udeal-zero-browser-ops-2026-07-21.md](./07-udeal-zero-browser-ops-2026-07-21.md) 为准。
- README 描述 Grok Web SSO 不可自动续期；本项目可以保活、同步和重新导入，但不能从已失效 SSO 凭空生成新凭据。
- README 未详细说明 Panda 的生产资源限制，Panda 操作以本维护文档和全局 `panda-remote-ops` 规则为准。
- 旧文「生产/恢复/隔离/退役七池」已过时；以本文件四池 + 双探针为准。
