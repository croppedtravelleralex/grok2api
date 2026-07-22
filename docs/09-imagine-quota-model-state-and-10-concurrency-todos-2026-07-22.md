# Imagine 额度、模型状态与 10 并发生图接续清单（2026-07-22）

## 目的与口径

本文是 Web Lite 生图当前工作的唯一接续清单，覆盖三件事：

1. 读取和刷新账号的 Imagine 周期最大次数与剩余次数；
2. 建立账号×模型的独立持久化状态，并用于图池选号；
3. 在已有 10 槽流水线基础上完成真实 `1→2→4→10` 并发生图验收。

状态标记：`DONE`=代码和本地验证已完成；`PARTIAL`=能力存在但生产或端到端验收未完成；`TODO`=尚未实现；`BLOCKED-UPSTREAM`=上游没有提供足够数据，不能靠本地代码准确补齐。

本文中的“10 并发已实现”只表示 10 个客户端请求可进入流水线；“10/10 已验收”必须是同一档 10 个请求全部拿到有效图片。两者不得混写。

## 已确认的上游事实

### `DONE` Imagine 次数接口

- 当前 Grok SPA 使用 `GET /rest/usage/free-usage-gates`。
- 响应包含 `chat/imagine/voice/build` 四个 gate；每个 gate 有 `allowance` 与 `remaining`。
- 字段可能是 JSON 字符串，也可能是数值，解析器已同时兼容。
- 单账号及 3 个账号只读请求均返回 HTTP 200。
- 多个账号的 Imagine 实测为 `allowance=0, remaining=0`；历史 Lite 真实成功账号也出现 `0/0`。

因此当前唯一安全语义是：

| 返回 | 本地语义 | 是否阻断生图 |
| --- | --- | --- |
| `total>0, remaining>0` | 额度明确可用，但尚不等于真实生图已成功 | 否 |
| `total>0, remaining=0` | 明确额度耗尽 | 是 |
| `0/0` | 免费闸门不适用或上限未知 | 否，按 unknown 低优先级探测 |
| 未同步/请求失败 | 未知 | 否，按 unknown 低优先级探测 |

### `BLOCKED-UPSTREAM` “每日几点刷新”

该接口没有返回窗口秒数、周期类型或绝对 ResetAt。因此当前可以准确读取“上游此刻声明的最大次数/剩余次数”，但不能准确承诺它一定是自然日额度，也不能给出每日几点刷新。

禁止用本地 `now+24h` 伪造 ResetAt。后续只能通过跨时段观测 allowance/remaining 变化来推断，且推断结果必须标为观测结论，不能冒充上游字段。

## 已完成的代码

### `DONE` 额度读取、刷新和存储

- `backend/internal/infra/provider/web/quota.go`
  - `SyncQuota` 会额外同步 `mode=imagine`。
  - `SyncQuotaMode("imagine")` 调用 `/rest/usage/free-usage-gates`。
  - 保存 `allowance→Total`、`remaining→Remaining`。
  - `WindowSeconds=0`、`ResetAt=nil`，不猜重置时间。
- `backend/internal/infra/provider/web/adapter.go`
  - Lite `grok-imagine-image` 的调度额度模式改为 `imagine`。
  - 上游实际协议模型仍保持 catalog 中的 `imagine-lite/fast`，没有混淆协议字段与调度额度字段。
- 单账号 `POST /api/admin/v1/accounts/:id/refresh-quota` 和全量 Web 额度同步会通过现有服务流程刷新并保存 Imagine 窗口。

### `DONE` `0/0` 防误杀

- Selector 仅在 `QuotaWindow.Total>0 && Remaining<=0` 时判定模型额度耗尽。
- 付费账号同时有 weekly 与 Imagine 窗口时，生图显式使用 Imagine；聊天仍按原 weekly/auto/fast 规则。
- 图池同样只把 `total>0 && remaining=0` 当作明确耗尽；`0/0` 和未同步状态仍允许低优先级真实探测。

### `DONE` 账号×模型独立状态表

- 新表：`account_model_states`。
- 主键：`(account_id, upstream_model)`。
- 状态枚举：

| 状态 | 含义 | 写入来源 |
| --- | --- | --- |
| `unknown` | 无明确额度或真实请求证据 | API 展示合成 |
| `quota_available` | 上游明确返回正额度，但尚未真实成功 | API 展示合成 |
| `available` | 当前模型真实请求成功 | 图片请求 finalize |
| `soft_stop` | SSE/上游无图软停止，处于模型级退避 | 图片错误策略 |
| `quota_exhausted` | 明确 Imagine 额度为零或上游 `usage_limit_reached` | 额度合成/429 |
| `auth_failed` | 图片请求最终 401 | 图片响应分类 |
| `signature_failed` | 最终 403 且 `anti_bot_rejected`/code 7 | 图片响应分类 |

- 持久字段：失败原因、连续失败次数、最近尝试、最近成功、冷却截止、更新时间。
- `LastSuccessAt` 在后续 soft-stop/失败更新时保留。
- 模型状态不修改账号全局认证/健康状态；网络错误不会被写成认证或签名失败。

### `DONE` 跨重启选号与图池策略

- Selector 重启后仍能从数据库恢复当前模型的排序证据。
- 同优先级的基础顺序：近期真实成功 → 未知/正额度待探测 → 活动 soft-stop。
- 运行时最新结果会覆盖持久化排序。
- 图池不再使用聊天 `fast` 剩余次数代表生图能力：
  - `available` 优先；
  - Imagine 明确正额度/`quota_available` 次之；
  - `0/0`/未同步 unknown 仍可进池但不冒充已验证；
  - 明确耗尽、认证失败、签名失败、活动 soft-stop 不进图池；
  - 新同步到的正额度可解除旧的 `quota_exhausted` 调度阻断；
  - Imagine 失败不把整个账号踢出聊天池。
- 同账号 Lite 并发仍固定为 1，避免 `too many requests in progress`。

### `DONE` 管理 API 与 Accounts 页面

- Account View/API 新增 `modelStates`。
- 返回字段：`upstreamModel/status/reason/consecutiveFailures/lastAttemptAt/lastSuccessAt/cooldownUntil/updatedAt`。
- Accounts 前端 DTO 与运行时 validator 覆盖全部 7 个状态，避免后端加字段后被判为无效响应。
- Grok Web 额度列新增独立 Imagine 行：显示剩余/总次数、状态 badge、重置时间说明。
- `0/0` 显示“次数上限未知”，Tooltip 明确说明它不代表耗尽。
- 中英文状态文案已补齐。

### `PARTIAL` 10 并发流水线

已有并已部署的基础能力：

- 10 个客户端 pipeline slots；
- 100 长度显式 FIFO/aging 准入队列；
- Expand 并发 2；
- SSE AIMD 初始 1、上限 6；
- Download 并发 8；
- SSE 后提前释放账号 lease；
- Timeline 显示 slot owner、queue/expand/SSE/download 等待和耗时；
- 本地 pipeline 满 429、上游 `usage_limit_reached` 429、soft-stop/502 已分类。

尚未完成的关键结果：生产没有 10 个被真实证明可生图的 fresh 账号，最近单请求已经会在约 27–35 秒后返回 `usage_limit_reached`。因此尚未运行有效的 4/10、10/10 成功档，不能声称“10 并发生图成功”。

## 本地验证证据

### `DONE`

- Imagine 配额解析与 `0/0` 语义单元测试。
- 模型状态 available→soft-stop 更新、保留 LastSuccessAt、RoutingCandidate hydrate 测试。
- Selector 重启后排序测试。
- 图池 `0/0` 可路由、明确 0/10 阻断、正额度解除旧 exhaustion、签名失败/soft-stop 阻断测试。
- Account API `modelStates` 转换测试。
- `go test ./...` 已通过。
- `pnpm build` 已通过。

### `DONE` 收尾验证

- `go vet ./...` 通过。
- `pnpm lint` 通过。
- `pnpm build` 通过。
- 最终一次 `go test ./...` 通过。
- `git diff --check` 通过，仅输出 Windows 工作树 LF→CRLF 提示，无空白错误。
- 已保持工作树中用户的 Build 四池、browser-bridge 和工具脚本改动；本轮未批量 stage、未提交。

## 明确未完成事项

### `TODO-P0` 发布与迁移

- 尚未提交、push 或触发 CI。
- 尚未部署到 Panda。
- 生产数据库尚未执行 `account_model_states` 自动迁移。
- 生产 Accounts 页面尚未看到 Imagine 行和模型状态。
- 生产单账号/全账号 Imagine 额度尚未通过新接口刷新。

### `TODO-P0` 生产安全 canary

- 部署前按 `panda-remote-ops` 做容量 preflight 和数据库备份。
- 只部署主服务镜像，不在 Panda 编译，不启动 browser-bridge。
- 先选 1 个历史 Lite 成功账号：刷新额度 → 检查 DB/API/UI → 发 1 次 Lite → 检查状态从 unknown/quota_available 变为 available 或准确失败分类。
- 验证 `0/0` 账号仍可路由，且不会被 Web 图池或 Selector 误踢。
- 验证 Imagine 失败不会影响同账号聊天路由。
- 验证回滚旧镜像时新增表不会破坏旧版本读取；回滚前保留数据库备份。

### `TODO-P0` 建立可用 Imagine 账号池

- 对现有 enabled Web 账号逐号、单并发、低速做 Lite canary。
- 每号至少记录：匿名 account ID、quota total/remaining、模型状态、HTTP 状态、upstream code、soft-stop、总耗时、最近成功时间。
- 账号分为：真实成功、明确额度耗尽、soft-stop、认证失败、签名失败、未知。
- 达到至少 10 个近期真实成功且不在冷却的账号后，才进入 10 并发验收。
- 不得用“聊天 active”“`/rest/modes` 200”或 Imagine `0/0` 代替真实生图成功证据。

### `TODO-P1` 分档并发验收

严格执行 `1→2→4→10`，上档未通过不得升档。每档记录：

- success/total；
- P50/P90/max wall time；
- 本地 pipeline 429、上游 429、soft-stop、401、anti-bot 403 数量；
- 实际使用的匿名账号数量，确认同账号并发始终为 1；
- queue/expand/SSE/download 等待和执行时间；
- SSE AIMD target 变化；
- 主服务 CPU、内存、load、health；
- 单住宅上行/下行带宽。

建议通过门槛：每档请求全部得到有效图片、无本地 pipeline 429、无同账号并发冲突、主服务无健康/资源停止线；否则停止升级并按失败类别修账号池、signer 或队列策略。

### `TODO-P1` 基于结果继续优化调度与上下行

- 为真实成功、额度待探测、未知账号设置可观测的分层计数和选中率。
- 检查“近期成功优先”是否造成热点；必要时在 available 组内按最久未选/剩余额度做轮转，而不是持续压同一批账号。
- 429 usage limit 后立即写模型 block，并验证正额度刷新或 block 到期能恢复。
- soft-stop 保持 30 秒指数退避、最高 5 分钟；观察是否需要按账号和出口分别统计。
- SSE 并发继续由 AIMD 控制，不因 10 slots 直接固定为 10；只有连续成功且出口资源安全才上探。
- 下图继续走独立 asset 并发 8；若启用高带宽 `grok_web_asset`，先做单图 canary，上传/握手仍保持同 Web 出口身份。
- Edit/Video 不纳入本轮 10 并发，不得顺带启用。

### `TODO-P1` signer 产品化

- 将 `/sign` 做成 compose sidecar 和可发布镜像，移除 `/tmp` 手工启动依赖。
- 自动发现 signer 模块和索引，避免 `[38,33,24,32]` 再漂移时静默失效。
- 保存 matched pair 时不记录 seed、HEX、SSO、Cookie 明文。
- `/healthz` 不能只检查进程；重建后必须用真实 `/rest/modes` 验证上游接受签名。
- code 7 集中爆发时先隔离 signer/出口问题，不批量把账号标为认证失败。

### `TODO-P2` 额度周期观测

- 以低频任务记录 allowance/remaining 的变化时间，不主动消耗图片额度探测重置。
- 观察至少两个疑似周期，确认是否真为每日、滚动窗口或账户特定 gate。
- 如果上游未来返回窗口/ResetAt，再扩展 schema/API；在此之前页面保持“上游未返回重置时间”。

### `TODO-P2` 可观测性与维护清理

- 为 `modelStates` 增加状态汇总和趋势，避免只能逐账号看表格。
- 为状态写入失败增加指标/告警；当前状态持久化失败只写 warning，不使用户图片请求失败。
- 评估是否需要维护任务清理长期失效账号的陈旧模型状态；任何清理不得删除额度窗口或账号凭据。
- 补充 API 文档/Swagger（若当前生成链要求显式 schema 更新）。

## 执行顺序

1. 完成本地 vet/lint/diff 校验并提交精确变更。
2. CI 通过后，按 Panda 规则 preflight、备份、单镜像部署。
3. 单账号验证迁移、额度刷新、API/UI 与真实 Lite 状态闭环。
4. 逐号建立至少 10 个 fresh `available` 账号。
5. 执行 `1→2→4→10` 分档门禁。
6. 依据 Timeline 和失败分类微调 available 组轮转、SSE AIMD 与 asset 下行。
7. 再做 signer sidecar、额度周期长期观测和状态汇总。

## 完成定义

只有同时满足以下条件，才能关闭本清单：

- 生产能读取并刷新 Imagine allowance/remaining，且 `0/0` 不被误判耗尽；
- 模型状态在重启后保留，成功/soft-stop/429/401/code 7 能准确更新；
- Accounts 页面准确显示次数、状态和“ResetAt 未知”；
- 图池不再借聊天 fast 次数判断生图能力；
- 至少 10 个账号有近期真实生图成功证据；
- 生产 `1→2→4→10` 全部通过，10/10 返回有效图片并留存指标；
- signer 重启后能通过真实上游健康检查；
- 文档记录最终生产镜像、CI、数据库迁移、canary 指标和回滚点。
