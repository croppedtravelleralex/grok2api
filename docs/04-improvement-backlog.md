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
| FE-003 | 号池循环探活可视化 | `https://grokimage.relai.asia/accounts` 上大量「待验证」，systemd/脚本探活只在 journal，前端看不见进度 | 账号页增加探活任务面板：总数/已完成/成功/失败、当前账号、并发、阶段（refresh_token/billing/web_quota）、可取消；复用现有 `AccountTaskProgressDTO` SSE/流式进度模式 | P0 | Planned | 2026-07-16 用户要求；截图全表待验证；后端探针 `/opt/grok2api/tools/grok2api-account-pool-probe.py` 已能登录跑通 |

## 后端

| ID | 标题 | 现象/问题 | 建议方向 | 优先级 | 状态 | 备注/证据 |
| --- | --- | --- | --- | --- | --- | --- |
| BE-001 | WebP 尺寸解析 | Go 标准库未内置 WebP DecodeConfig | 在确认真实 WebP 资产后引入受控解析 | P2 | Idea | PNG/JPEG/GIF 已支持 |
| BE-002 | 旧图片尺寸回填 | 当前旧图片每次列表会按需读取文件 | 增加后台低速一次性回填或读取后持久化 | P2 | Idea | 当前图片量很小，不阻塞上线 |
| BE-003 | Build 凭据恢复 | `invalid_grant` 无法靠旧 RT 恢复，权限拒绝曾被刷新重新激活 | 新 RT 重导入；权限拒绝保持隔离；生产仅选已验证账号 | P0 | Done | 单并发后台探针按优先级和 ID 顺序循环；候选存在时 30 秒 1 个，扫空后 5 分钟巡检 |
| BE-004 | Messages 用量与模型归一化 | 流式请求输入/缓存为 0，实际模型暴露 `-build-free` | 尾事件发送完整 usage，对外固定公开模型，内部保留观测型号 | P0 | Done | 后端全量测试通过 |
| BE-005 | Build 多池恢复调度 | 权限拒绝只能停在失效态或被直接删除 | 隔离后串行恢复、分级退避、软退役、重导入复活 | P0 | Done | 恢复池与待验证池交替，最大并发 1 |
| BE-006 | Web 桥接故障隔离 | 桥接停机会把本机连接失败误记为代理 `transport error`，冷却整个 Web 池 | 桥接不可达只记录控制面告警，不反馈代理健康；真实 403/代理故障继续反馈 | P0 | Done | 单元测试覆盖连接拒绝分类，生产仅做单账号 canary |
| BE-007 | 探活任务控制面 API | 探活在主机 systemd timer，管理端无法启动/观测/取消 | 新增 admin API：`POST /accounts/probe-cycle`（concurrency 1–5）、`GET` 进度、`DELETE` 取消；状态落库或内存+审计；前端 FE-003 消费 | P0 | Planned | 与 FE-003 成对；现有导入/强制刷新已有 `runAccountTask` 流式进度可参考 |
| BE-008 | Grok Web HTTP 逆向（类 gptimage） | 当前 Web/生图每请求起 browser-bridge Chrome，Panda 资源与 CF 成本极高 | 对照 `/root/gptimage`：`curl_cffi` 伪装 + sentinel/会话 HTTP API；浏览器仅做 CF clearance；目标去掉每请求 Chromium | P0 | Planned | gptimage 用 `openai_backend_api.py` + conversation/files/tasks；grok 需单独抓包，不能直接抄 ChatGPT 路径 |

## 稳定性与可维护性

| ID | 标题 | 现象/问题 | 建议方向 | 优先级 | 状态 | 备注/证据 |
| --- | --- | --- | --- | --- | --- | --- |
| MTN-001 | Web 浏览器会话 canary | 代理连接正常但 Grok 直连返回 CF 403，浏览器加载阶段超时 | 保持单浏览器；更换住宅/ISP 节点后复测 | P0 | In Progress | 当前桥接已停止，禁止全量刷新 |
| MTN-004 | 桥接失败上限 | 失败 canary 会占用约 75 秒 | 启动上限 30 秒、阶段错误、结构化 502 | P0 | Done | 脚本和 Compose 已部署；桥接默认保持停止 |
| MTN-002 | Web 并发逐级容量测试 | 单并发稳定但吞吐受限 | 1→2 逐级测试 CPU/内存/403 率 | P1 | Planned | 必须遵守 Panda 门槛 |
| MTN-003 | 账号错误趋势 | 目前主要看当前状态 | 记录错误码、刷新成功率和恢复时长趋势 | P1 | Idea | 不记录凭据正文 |
| MTN-005 | Webshare 出口替换 | 100 节点集中在 10 个新加坡机房网段，Grok CF 403 | 采用粘滞住宅/ISP 节点；同账号固定 IP、UA、持久化浏览器 profile | P0 | Planned | Panda 单节点已证明白名单正常但 Grok 会话被拒绝 |
| MTN-007 | CF 403：直连 vs Webshare 对照 + 拒因深挖 | 用户问 panda 直连 403 时 webshare 是否也 403；需分清挑战类型 | 保留对照证据；拒因定为 Managed Challenge；换稳定出口 + 浏览器求解 | P0 | Done（拒因证据）/ Planned（换出口） | 深挖：`cf-mitigated=challenge` + 正文 `cType=managed`；本机 GSL 机房段可 pass；腾讯云 SG / Webshare 否；脚本 `grok-cf-deep-diagnose.py` |
| MTN-008 | udeal 出口筛池 + canary | Desktop `udeal1000proxy.txt` 是否比 Webshare 更能过 grok CF | 实时筛 `pass_app`；粘滞 session 不可永久信任；合格出口做 bridge canary | P0 | In Progress | 早盘 80 条 ok_200=3；深挖时原 3 session 全变 challenge；新鲜 40 条 **pass=1**（`6XIJ`→`82.47.112.28` Webgist/GB，复测仍 200/MRS） |
| MTN-006 | 独立浏览器 Worker | Panda 启动真实 Chromium 12 秒后 load1 达 2.10，触发低资源硬门槛 | 将现有认证桥接部署到独立 2C/4G 以上 worker，通过私网/Tailscale 供 Panda 调用 | P0 | Planned | Panda 只保留 Go 网关；worker 单浏览器、单会话、粘滞代理 |
| MTN-006 | 浏览器身份连续性 | 生产配置每次销毁会话且桥接忽略出口 UA | 会话复用 30 分钟；UA、平台、代理、Cookie 纳入同一会话身份 | P0 | In Progress | 本地 13 项桥接测试通过，待 Panda 单账号 canary |
