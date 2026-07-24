# grokImage 长期改进建议池

状态词固定为：`Idea`、`Planned`、`In Progress`、`Done`、`Dropped`。

## 产品体验

| ID | 标题 | 现象/问题 | 建议方向 | 优先级 | 状态 | 备注/证据 |
| --- | --- | --- | --- | --- | --- | --- |
| UX-001 | 图片日期管理 | 历史图片只能逐张删除，时间精度不足 | 日期筛选、分组、按日删除、全部删除、秒级时间 | P0 | Done | 本轮本地测试/构建通过 |
| UX-002 | Build 异常原因可操作化 | 统一“失效”无法区分 RT 撤销和无权限 | 状态提示展示恢复动作 | P0 | Done | `invalid_grant` 与 `access denied` 已分流 |

## 前端

| ID | 标题 | 现象/问题 | 建议方向 | 优先级 | 状态 | 备注/证据 |
| --- | --- | --- | --- | --- | --- | --- |
| FE-001 | 图片详情扩展 | 旧页面只有时间、大小和 MIME | 展示耗时、实际/请求分辨率、模型 | P0 | Done | 旧图片耗时显示未知 |
| FE-002 | 图片详情抽屉 | 卡片信息继续增加后空间有限 | 后续增加单图详情抽屉和 Request Audit 跳转 | P2 | Idea | 依赖 requestId |
| FE-003 | 号池循环探活可视化 | `https://grokimage.relai.asia/accounts` 上大量「待验证」，后台探活只在日志可见 | 账号页展示当前/最近账号、验证/恢复阶段、调度时间、成功失败、七类池统计与最近结果 | P0 | Done | **2026-07-22**：已演进为四池面板；细节与后续缺口见 [08](./08-build-four-pool-dual-probe-todos-2026-07-22.md) |

## 后端

| ID | 标题 | 现象/问题 | 建议方向 | 优先级 | 状态 | 备注/证据 |
| --- | --- | --- | --- | --- | --- | --- |
| BE-001 | WebP 尺寸解析 | Go 标准库未内置 WebP DecodeConfig | 在确认真实 WebP 资产后引入受控解析 | P2 | Idea | PNG/JPEG/GIF 已支持 |
| BE-002 | 旧图片尺寸回填 | 当前旧图片每次列表会按需读取文件 | 增加后台低速一次性回填或读取后持久化 | P2 | Idea | 当前图片量很小，不阻塞上线 |
| BE-003 | Build 凭据恢复 | `invalid_grant` 无法靠旧 RT 恢复，权限拒绝曾被刷新重新激活 | 新 RT 重导入；权限拒绝保持隔离；生产仅选已验证账号 | P0 | Done | 单并发后台探针按优先级和 ID 顺序循环；候选存在时 30 秒 1 个，扫空后 5 分钟巡检 |
| BE-004 | Messages 用量与模型归一化 | 流式请求输入/缓存为 0，实际模型暴露 `-build-free` | 尾事件发送完整 usage，对外固定公开模型，内部保留观测型号 | P0 | Done | 后端全量测试通过 |
| BE-005 | Build 多池恢复调度 | 权限拒绝只能停在失效态或被直接删除 | 隔离后串行恢复、分级退避、软退役、重导入复活 | P0 | Dropped | **被四池方案取代**：取消恢复/软退役；见 BE-009 与 [08](./08-build-four-pool-dual-probe-todos-2026-07-22.md) |
| BE-006 | Web 桥接故障隔离 | 桥接停机会把本机连接失败误记为代理 `transport error`，冷却整个 Web 池 | 桥接不可达只记录控制面告警，不反馈代理健康；真实 403/代理故障继续反馈 | P0 | Done | 单元测试覆盖连接拒绝分类，生产仅做单账号 canary |
| BE-007 | 探活状态 API | 后台循环探针无法从管理端观测 | 新增只读 `GET /accounts/build-probe`，暴露当前账号、阶段、调度、运行期统计、分池和最近结果 | P0 | Done | 已四池化；Panda 双探针合计并发 ≤2 |
| BE-008 | Grok Web HTTP 逆向（类 gptimage） | 原先 Web/生图每请求起 browser-bridge Chrome，Panda 资源与 CF 成本极高 | 纯 HTTP 首页 meta + 每请求新 Statsig 票 + HTTP Chat/Lite；浏览器仅清障 | P0 | In Progress | 本轮本地已取消最终票缓存并补齐 FIFO/槽位；当前生产镜像 80/80 403，必须以新镜像 canary 结果为准；签名器镜像内置/受管 sidecar 仍待做 |
| BE-009 | Build 四池 + 双探针 | 恢复池饿死删除；死号不物理删；选号全表扫 | 调度/普通/验证/删除；双探针；删除池物理删；DispatchIndex+DRR | P0 | Done（主路径）/ In Progress（增强） | 主路径 2026-07-21 上线；删除池 2026-07-22 已清零。剩余 FP-001～015 见 [08](./08-build-four-pool-dual-probe-todos-2026-07-22.md) |
| BE-010 | Build 选号热路径去全表 | 仍 `ListRoutingCandidates` 全量后再按 DispatchIndex 重排 | Acquire 只取索引前 k 再 hydrate | P1 | Todo | = FP-001 |
| BE-011 | DispatchIndex 真实额度序 | 索引 Upsert 未写入 quota_remaining | 同步 billing/recovery 到索引 key | P1 | Todo | = FP-002 |
| BE-012 | 探针 DRR 占比可观测 | 生产无法验证 5:3:2 / 7:3 | statistics 增加 per-lane 计数 | P1 | Todo | = FP-003 |
| BE-013 | 清理恢复/purge 仓储死 API | `ListRecoveryCandidates`/`ListPurgeCandidates` 仍在 | 删除或正式 Deprecated 且无调用 | P1 | Todo | = FP-004 |
| BE-014 | Web Lite asset 下载 403 | 无票路径曾 403；有票路径已修 | SSE 前 warm + Python 下载头（`c07cc2e`） | P0 | Done（有票）/ In Progress（无票回退） | [12](./12-web-lite-two-stage-failure-asset-403-2026-07-23.md)；[13](./13-chrome-ticket-pool-panda-api-2026-07-23.md) |
| BE-015 | Chrome 票池 + grok2api 生图 | 本机 Chrome 批捕 meta；Go 票池 + signer 现签 | 已部署 `c07cc2e`；持续 minter + 生命周期实验 | P1 | In Progress | [13](./13-chrome-ticket-pool-panda-api-2026-07-23.md)；[14](./14-chrome-ticket-lifecycle-experiments-2026-07-23.md) |
| BE-016 | Chrome 票生命周期量化 | R-delay pass；S 下限 **≥60min**；S-3h 待 consume；V 4/5 | P1 | **Frozen** | 冻结至 [plan.md](./plan.md) §4 门禁；[16](./16-chrome-ticket-experiments-round2-2026-07-23.md) |
| BE-017 | Admin 密码三源合一 | secrets / reset 硬编码 / import `.tmp` 不同步 → 401 与 import 200 并存 | 统一单一真相源；改密必写三处；禁用 reset 硬编码 | P1 | In Progress | 2026-07-23 已临时同步；见 [16](./16-chrome-ticket-experiments-round2-2026-07-23.md) |
| BE-018 | Egress 全量流量统计 | 仅 07-21 单口压测 + media 体积；无逐请求代理字节 | **Go** `Lease.Do` 插桩 + `egress_traffic_hops`；CLI 读数 PoC→**Rust** | P0 | Done | [plan.md](./plan.md) §1；`96b664d` |
| BE-019 | Web 号池单一真相源 | pin / imagePoolIds / dispatchIndex 三套不一致 → 503 | **Go** 索引∩pin + sync；运维 **Python PoC→Rust** pool-ops | P0 | Done | Phase B：`pinNotInDispatch=[]`；生图仍 429/502 |
| BE-020 | Selector 诊断与指标 | 503 难区分 saturated vs 真空池 | `selection_reason` + dispatch/pin/stale 指标 | P1 | Done | `X-Grok-Selection-Reason` |
| BE-021 | 票池与选号联动 | 选号不查票 → 无票回退 | Acquire 偏好有票；daemon 池深跟 SSESlots | P1 | Done | selector 有票优先；mint daemon 联动 |
| BE-023 | Web Image 四池 | 软停/耗尽号进 dispatch；pin 洗状态 | 对齐 Build 四池；dispatch=`imageDispatchAdmissible` | P0 | Done | `web_pool_probe.go`；pin 脚本仅绑 route |
| BE-022 | 运维/实验工具 Rust 化 | Python 脚本当生产依赖 | PoC 冻结 JSON 契约后：`grok-pool-ops-rs`、`grok-ticket-minter-rs`、`grok-experiment-rs` | P1 | Planned | [plan.md](./plan.md) 语言分层；参考 `web_http_chat_image_canary_rs` |

## 稳定性与可维护性

| ID | 标题 | 现象/问题 | 建议方向 | 优先级 | 状态 | 备注/证据 |
| --- | --- | --- | --- | --- | --- | --- |
| MTN-001 | Web 浏览器会话 canary | 代理连接正常但 Grok 直连返回 CF 403，浏览器加载阶段超时 | 保持单浏览器；更换住宅/ISP 节点后复测 | P2 | Superseded | **2026-07-21** 生产 Web 已改 udeal + 零浏览器，不再依赖 Panda 上 Chromium canary；桥接保持停止 |
| MTN-004 | 桥接失败上限 | 失败 canary 会占用约 75 秒 | 启动上限 30 秒、阶段错误、结构化 502 | P0 | Done | 脚本和 Compose 已部署；桥接默认保持停止 |
| MTN-002 | Web 并发逐级容量测试 | 单并发稳定但吞吐受限 | 1→2 逐级测试 CPU/内存/403 率 | P1 | Planned | 必须遵守 Panda 门槛 |
| MTN-003 | 账号错误趋势 | 目前主要看当前状态 | 记录错误码、刷新成功率和恢复时长趋势 | P1 | Idea | 不记录凭据正文 |
| MTN-005 | Webshare 出口替换 | 100 节点集中在 10 个新加坡机房网段，Grok CF 403 | 采用粘滞住宅/ISP 节点；同账号固定 IP、UA、持久化浏览器 profile | P0 | Planned | Panda 单节点已证明白名单正常但 Grok 会话被拒绝 |
| MTN-007 | CF 403：直连 vs Webshare 对照 + 拒因深挖 | 用户问 panda 直连 403 时 webshare 是否也 403；需分清挑战类型 | 保留对照证据；拒因定为 Managed Challenge；换稳定出口 + 浏览器求解 | P0 | Done（拒因证据）/ Planned（换出口） | 深挖：`cf-mitigated=challenge` + 正文 `cType=managed`；本机 GSL 机房段可 pass；腾讯云 SG / Webshare 否；脚本 `grok-cf-deep-diagnose.py` |
| MTN-008 | udeal 出口筛池 + canary | Desktop `udeal1000proxy.txt` 是否比 Webshare 更能过 grok CF | 实时筛 `pass_app`；粘滞 session 不可永久信任；合格出口做 bridge canary | P0 | Done（LA 固定口） | **2026-07-21**：用户指定 LA 住宅 `70.39.164.200:30000` 稳定过 CF；池筛 `as.udealproxy.com` 仍稀。生产 Web 仅挂该口；Webshare 100/100 challenge |
| MTN-006 | 独立浏览器 Worker | Panda 启动真实 Chromium 12 秒后 load1 达 2.10，触发低资源硬门槛 | 将现有认证桥接部署到独立 2C/4G 以上 worker，通过私网/Tailscale 供 Panda 调用 | P0 | Planned | Panda 只保留 Go 网关；worker 单浏览器、单会话、粘滞代理 |
| MTN-006 | 浏览器身份连续性 | 生产配置每次销毁会话且桥接忽略出口 UA | 会话复用 30 分钟；UA、平台、代理、Cookie 纳入同一会话身份 | P0 | In Progress | 本地 13 项桥接测试通过，待 Panda 单账号 canary |
