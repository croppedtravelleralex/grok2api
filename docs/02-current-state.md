# grokImage 当前状态主档

## 最后更新时间

- 日期：2026-07-22
- 维护目的：记录 Build 四池主路径已清零删除池；后续增强待办见 [08](./08-build-four-pool-dual-probe-todos-2026-07-22.md)。

## 整体状态摘要

- 后端为 Go 网关，前端为 React/Vite 管理端，支持 Grok Build、Web、Console 三个账号池。
- Panda 为低资源生产机：**禁止在其上编译/构建**；标准链为本地改测 → GitHub 上传（Actions/GHCR）→ Panda 仅 `pull` 运行。
- **Build（2026-07-22）**：四池主路径已上线；生产快照约 `dispatch≈211 / normal=2 / verification=0 / delete=0`。已做/未做全量盘点与 FP-* 待办见 [08-build-four-pool-dual-probe-todos-2026-07-22.md](./08-build-four-pool-dual-probe-todos-2026-07-22.md)。
- **Web（2026-07-22）**：Lite 主路径已切纯 HTTP——关 browser-bridge + 本地 signer（`127.0.0.1:8788/sign`）+ 单住宅出口；文本与 Lite 均有 200 证据。当前生产镜像为 `5da879fc…15295` / `613a305`。
- **Imagine 次数读取（本地已实现、生产未部署）**：`GET /rest/usage/free-usage-gates` 可返回 `imagine.allowance/remaining`；上游不返回窗口长度或绝对重置时间。实测多个账号（含历史 Lite 成功号）均可能返回 `0/0`，因此 `0/0` 定义为“免费闸门不适用或上限未知”，只有 `total>0 && remaining=0` 才能判定额度耗尽。
- **账号×模型独立状态（本地已实现、生产未部署）**：持久化 `unknown / quota_available / available / soft_stop / quota_exhausted / auth_failed / signature_failed`；额度窗口与真实请求结果分开保存。Accounts API/页面会同时展示 Imagine 次数和模型状态。
- 本轮已做、未做、生产门禁与下一步完成定义统一见 [09-imagine-quota-model-state-and-10-concurrency-todos-2026-07-22.md](./09-imagine-quota-model-state-and-10-concurrency-todos-2026-07-22.md)。
- Web 调度当前 **20 个账号**进池：`641,642,644,646,647,649,650,652,654,656` + `659,661,663,667,669,671,673,674,675,677`；文生图开、图生图/视频关；NewAPI 渠道 `#105` 已启用。
- NewAPI `#105` BaseURL：`http://grok2api:8000`（容器直连，避开 CF；需 `new-api` 加入 `grok2api_default` 网络）。
- **NewAPI 文生图已验收**：同机 e2e 曾 **200**（约 7–11s），媒体 URL 落在 `https://grokimage.relai.asia/v1/media/images/...`（token 须 `group=grok`、DB key 48 位无连字符、无 `sk-` 前缀）；池薄时仍会 `429 usage_limit`。
- **10 并发状态**：10 槽/100 queue、Expand 2、SSE AIMD 1→6、Download 8 已部署；这表示 10 个客户端请求可同时进入流水线，不表示单出口同时发 10 条 SSE。最终单请求仍连续 `usage_limit_reached`，所以 4/10 生产档未运行，10/10 成功尚未验收。
- NewAPI 图片名称仍为 `grok-imagine-image` / `quality` / `edit`；edit/video 渠道保持 disabled。

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

- **P0（2026-07-21）**：部署四池双探针后观察删除上涨与调度池稳定；外挂 timer 已收窄。
- 历史待办 [06-open-todos-2026-07-16.md](./06-open-todos-2026-07-16.md)：探活可视化已完成。

## 已知阻塞与风险

- **Web 主路径架构已切纯 HTTP，但 10/10 生产验收未通过（2026-07-22）**：当前依赖 Panda `/tmp` signer + 单住宅出口。fresh browser matched pair 已消除 code 7；最终两次单请求分别 27.04s/34.97s 后返回真实 429，因此按门禁停止，未运行 2/4/10。
- signer 静态索引已从 `[31,16,43,8]` 漂移到 `[38,33,24,32]`；更严重的是 8 分钟动态 challenge 重算会生成服务端不接受的 pair。当前临时使用一次性 Chrome 捕获的 matched seed+HEX，并把刷新窗口放宽到 24h。signer 仍未镜像化，也没有“真实签名被上游接受”的强健康检查。
- Webshare **不可**作 `grok_web`（CF Managed Challenge）；可仅试验 `grok_web_asset` CDN 下图。
- 单住宅出口：上行 ~1.5 Mbps / 下行 ~15 Mbps；Lite 出图约 **170KB JPEG / 784×1168**。当前保持 `webConcurrency=2`、`assetConcurrency=8`、`expandConcurrency=2`、`mediaConcurrency=1`，SSE target 从 1 上探；账号接受率恢复前不得把 web/SSE 下限抬高。
- Lite 文生图：准入排队 → 扩写池 → SSE AIMD → 下图池；账号 lease 在 SSE 后早释；`/image-timeline` 甘特图。
- 图生图暂关；开启前需额度与冷却策略，且同口上传应串行。

## 下一步 3-5 项

1. 对 Web dispatch 账号做低速、逐号 Lite 能力 canary，建立至少 10 个 fresh 模型成功证据；不要用聊天 active 代替生图可用。
2. 先部署并单账号验证 Imagine 次数同步和模型状态迁移；确认 Accounts 页不会把 `0/0` 显示成耗尽，再按新图池排序观察调度。
3. 将 signer 做成 compose sidecar：自动发现模块/索引、保存 matched pair、重建后强制真实 `/rest/modes` 验证，失败不放量。
4. 账号池达标后重新从 1→2→4→10 执行生产门禁，并记录 success/P50/P90/max/soft-stop/429。
5. `grok_web_asset` 挂高带宽口做下图 canary；上行握手/上传仍保持同出口，并继续观察 Build 四池。

## 与 README 或旧文档的不一致处

- README / 旧 current-state 仍写「Web Cloudflare 不可用 / 强制 browser-bridge」——**已过时**；以本文件与 [07-udeal-zero-browser-ops-2026-07-21.md](./07-udeal-zero-browser-ops-2026-07-21.md) 为准。
- README 描述 Grok Web SSO 不可自动续期；本项目可以保活、同步和重新导入，但不能从已失效 SSO 凭空生成新凭据。
- README 未详细说明 Panda 的生产资源限制，Panda 操作以本维护文档和全局 `panda-remote-ops` 规则为准。
- 旧文「生产/恢复/隔离/退役七池」已过时；以本文件四池 + 双探针为准。
