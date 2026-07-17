# grokImage 当前状态主档

## 最后更新时间

- 日期：2026-07-17
- 维护目的：记录 Build 探针「扫到即续期+刷 Billing」、Messages 缓存 usage 拆分，以及 NewAPI `grok-4.5` 渠道调度修复。

## 整体状态摘要

- 后端为 Go 网关，前端为 React/Vite 管理端，支持 Grok Build、Web、Console 三个账号池。
- 本地工作分支为 `codex/panda-safe-completion`；Panda 已运行本轮镜像摘要 `sha256:4ce09f388c12...`（提交 `ff3f8da`）。
- Panda 为低资源生产机：**禁止在其上编译/构建**；标准链为本地改测 → GitHub 上传（Actions/GHCR）→ Panda 仅 `pull` 运行 → 按需清理 GHCR/临时仓库产物。
- NewAPI 已按 Chat Completions、Responses、Messages、Images 和 Videos 拆分接入；本轮不改其渠道结构。
- NewAPI 图片渠道已移除人为的 `gpt-image-*` 名称，统一为 `grok-imagine-image`、`grok-imagine-image-quality` 和 `grok-imagine-image-edit`；由于 Web/Cloudflare 仍不可用，generations/edits 暂时 disabled，模型列表不再暴露旧别名或不可用入口。

## 已完成功能

### 基础架构

- SQLite/PostgreSQL、内存/Redis 运行时、账号池、模型路由、请求审计和代理出口。
- Panda 域名入口为 `grokimage.relai.asia`，生产服务由容器运行。
- Web 与 Web Asset 使用分离的出口作用域；本轮新增各自全局单并发闸门，避免 Webshare/浏览器链路被并发打满，同时避免 WebSocket 与图片下载互相死锁。

### 核心业务能力

- 支持 Responses、Chat Completions、Anthropic Messages、图片生成/编辑和异步视频接口。
- Chat/Messages 对外响应模型固定为公开模型名，不再泄露 `grok-4.5-build-free` 这类上游内部型号；账号的真实观测型号仍保留在后端。
- Messages 流式尾事件会返回完整输入、输出和缓存命中 Token；Claude Code 的 `cache_control` 稳定前缀会派生粘滞键。
- Build `permission-denied` 不再伪装成客户端登录失效：账号被隔离，Messages 对外返回可重试的 503 `overloaded_error`。
- Build 调度已明确分为生产池、待验证池、隔离池、恢复池和退役池：权限拒绝立即进入隔离池；同一个单并发 worker 在恢复池与待验证池之间交替，避免死号恢复和新号验证互相饿死。
- Build 循环探针扫到账号时会：条件续期凭据（验证模式仅临近过期续期；恢复模式首次强制旋 RT）→ 刷 Billing（失败不阻断）→ 再做最小 grok-4.5 能力探测；单次超时放宽到 90 秒。
- Anthropic Messages：上游 `cached_tokens` 按 Anthropic 语义拆成 `input_tokens` + `cache_read_input_tokens`；Claude Code 的 `cache_control` 派生键会写入上游 `prompt_cache_key`，便于返回缓存命中。
- 隔离账号第一次恢复会优先尝试已关联 Web SSO，或最多旋转一次可用 RT，再执行最小 Build Chat 请求；前两次失败分别退避 15 分钟、1 小时，第 3 次失败后软退役。软退役保留 ID、关联和审计，重新导入新凭据会自动复活。
- Accounts 页已增加 Build 循环探针面板：每 5 秒读取只读状态，展示当前/最近账号、验证或恢复阶段、下次运行时间、连续失败、运行期累计成功失败、生产可信池占比、七类池数量和最近结果。
- 新增只读管理接口 `GET /api/admin/v1/accounts/build-probe`；统计保存在当前进程内，服务重启后从零累计，账号池数量始终从数据库实时汇总。
- 生产 canary 发现延迟返回的 OAuth 刷新失败会覆盖 `retired:` 终态；现已保护软退役标记，避免退役账号被误归为普通禁用，并保留重新导入新凭据后自动复活的语义。
- 手动“刷新并验证”不再只旋转 OAuth Token：单号和批量刷新成功后会立即发送最小 Build Chat 请求，成功写入 `observed_model` 并离开待验证池；权限或认证失败会立刻进入对应隔离/恢复状态。Panda 的批量凭据刷新并发固定为 1。
- 图片本地归档已记录请求 ID、模型、请求分辨率、实际宽高、生成耗时和精确到秒的时间。
- 图片管理已支持本地日期筛选、按日期分组、按日期删除和一键删除全部。
- 旧图片在读取时会从 PNG/JPEG/GIF 文件解析实际分辨率；旧图片无法反推出历史生成耗时，会显示未知。
- Web→Build 转换在已关联 Build 账号处于 `reauthRequired` 时会更新原账号凭据，而不是错误地跳过。

### 维护性与工程能力

- 后端 `go test ./...` 已通过。
- 前端 `pnpm lint` 与 `pnpm build` 已通过。
- 本地临时实例已完成数据库迁移和管理 API 验收：图片列表、日期筛选、图片读取、元数据展示和按日期删除均通过。
- 本地探针可视化验收通过：管理接口返回结构正确，桌面与 390px 视口均可显示且无横向溢出；前端控制台无新增运行错误。
- Panda 已部署镜像 `sha256:e64613c737c0...`：探针固定每 30 秒单并发运行，管理 API 已返回运行期计数、最近结果和完整七类池统计；主容器约 50 MiB、CPU 约 0.06%，浏览器桥接保持停止。
- 生产数据修复前已创建 SQLite 在线备份 `backend-before-retired-state-repair-20260716-212258.db`；先修 1 个 canary，再修复其余 45 条被 OAuth 延迟失败覆盖的软退役记录，持续观察后未再次被覆盖。
- 已建立 `docs/` 维护入口和 Panda 低资源操作规则。

## 账号池诊断事实

- 2026-07-16 部署循环探针后的最新 Build 快照：生产可信池 18、待验证可运行 82、待验证冷却 6、隔离/恢复 205、退役/禁用 0；总并发始终为 1。
- 2026-07-15 的历史异常分类为 85 个 OAuth `invalid_grant`、30 个 Build Chat `access denied`；后续大量新号导入后，当前 205 个隔离/恢复账号需按新探针结果重新统计错误分布，不能沿用旧比例。
- 2 个已有 Web 关联的 `invalid_grant` Build 账号已通过 Web→Build 转换换取并保存新凭据，账号 ID 与关联关系得到保留；随后能力探测确认两者仍缺少 Build Chat 权限，因此没有将其伪装为可用账号。
- 剩余 85 个 `invalid_grant` 必须导入新的有效 RT；30 个 `access denied` 账号继续隔离在调度池外，同 Token 刷新不会产生 Build 权限。
- 已修复 Token 刷新无条件把 `access denied` 账号恢复为 active 的缺陷；权限拒绝后不再强制刷新 RT。
- Build 在线请求优先且仅使用已有成功响应型号记录的账号；未验证账号不再借真实用户请求试错。诊断快照中 active 账号只有少量已有成功响应记录，因此后续必须使用单并发能力探测逐步扩大可信池。
- Panda 的 Build 能力探测配置为延迟 2 分钟启动、单并发按 `priority DESC, id ASC` 顺序循环；有候选时每 30 秒处理 1 个，扫空后每 5 分钟巡检。成功才进入可信池，权限拒绝转为 `reauthRequired`，临时错误冷却 15 分钟。
- 重新导入处于 `reauthRequired` 的 Build 账号时会清除旧 `observed_model`，防止旧权限结论污染新凭据；新凭据必须重新通过能力探测。
- 生产 Messages canary 已通过：NewAPI `/v1/messages` 返回 200，对外及 NewAPI 记录的上游模型均为 `grok-4.5`；最终 usage 正确记录输入和输出 Token。
- 首个后台 Build 能力探测已按计划只处理 1 个账号，并把确认无权限的账号转为 `reauthRequired`；探测后 grok2api 约 34–40 MiB，服务持续 healthy。
- 2026-07-16 新循环探针生产 canary 按约 30 秒严格串行执行：恢复池 156 → 待验证 127 → 恢复池 157 → 待验证 129/130；Panda 主服务约 54 MiB、CPU 0.07%、load1 0.03，公网与本机健康检查均为 200。
- Web 额度刷新应用层原本已有独立单并发池；Panda 单节点代理测试显示 Webshare 连接正常，但直接访问 Grok 返回 Cloudflare 403。
- 单浏览器、单账号额度 canary 在加载 Grok 页面阶段超时并返回 502；桥接容器已停止，未执行全量刷新。当前根因是所选代理上的 Cloudflare 浏览器会话无法建立，不是代理白名单或并发连接失败。
- 2026-07-15 复核发现 33 个 Web 出口曾全部显示 `transport error` 并进入冷却：桥接停止时，本机到桥接的连接失败被错误反馈成代理故障，导致 Web 请求在取得出口租约阶段即 502，根本没有到达 Chromium。后端现已把“桥接不可达”分类为控制面故障，不再扣减代理健康度；真实代理错误和上游 403 仍会正常降级。
- 深入代码审计确认桥接还有两个身份连续性缺陷：Panda 配置原先每次请求后销毁浏览器会话，且桥接忽略 Go 传入的出口 User-Agent。二者已在本地修复为同账号/代理/Cookie/UA 复用 30 分钟，并在浏览器启动前同步 UA 与平台；尚待生产单账号 canary。
- Panda 两份桥接密钥文件哈希一致，排除主服务与桥接鉴权密钥不一致；2026-07-15 只读预检为 2 核、load1 0.21、可用内存 2252 MiB、根盘 82%，主服务 healthy，桥接仍为停止状态。
- Webshare 100 个出口仅分布在 10 个 `/24`，集中于新加坡机房 ASN。Panda canary 已证明代理鉴权和外网连通正常、但 Grok 返回 Cloudflare 403；主要风险是机房 ASN 信誉、IP/账号地域不一致，以及 SSO/clearance 与原始 IP、UA、TLS/浏览器指纹不一致。

## 进行中事项

- 浏览器桥接的 30 秒启动上限、阶段化错误和结构化 502 已部署；会话复用与 UA/平台一致性修复已完成本地测试，等待低资源生产 canary。
- **P0 待办（2026-07-16，详见 [06-open-todos-2026-07-16.md](./06-open-todos-2026-07-16.md)）**
  1. 账号页循环探活只读可视化已完成；人工启动/取消和并发调节不进入 Panda 生产面板，固定单并发由后台持续运行 — FE-003 + BE-007
  2. Cloudflare 403：直连与 Webshare 对照证据已完成；换住宅/ISP 出口仍待做 — MTN-007 / MTN-005
  3. Grok Web/生图 HTTP 逆向（对齐 gptimage，去掉每请求 Chrome）— BE-008

## 2026-07-16 新增事实

- 管理端 Accounts 页大量「待验证」；循环探针现已通过页面和管理 API 可见，无需再登录主机读取 journal。
- 空代理 egress 生图仍 502（bootstrap ~103s）；有请求时 bridge 峰值约 CPU 76% / 内存 ~487MB。
- Cloudflare plain HTTP 对照：panda 直连与 Webshare 抽样 5 节点对 `grok.com` **全部 403 + Just a moment**（`plain_200_no_cf=0`）。
- **拒因深挖（2026-07-16）**：被拒页为 CF **Managed Challenge**（`cf-mitigated=challenge`，`cType=managed`），不是硬封文案；本机 GSL 机房段可 plain 200；udeal 偶发 `pass_app` 但 session 会漂（原 QmFKV 等已失效；新样本 `6XIJ`/Webgist GB 可过）。
- gptimage 生图走 `curl_cffi` HTTP，浏览器仅清障；grok2api 仍强制 browser-bridge。

## 已知阻塞与风险

- Grok Web 当前被 Cloudflare 浏览器会话建立失败阻塞；桥接停止是失败后的资源保护状态，不是 Cloudflare 403 的根因。
- **2026-07-16：** panda 直连与当前 Webshare 在 plain HTTP 层均返回 CF 403；空代理生图仍失败。换粘滞住宅/ISP +（长期）HTTP 逆向是主路径。
- 浏览器桥接资源上限已收紧到 0.75 CPU / 768 MiB，生产验证继续坚持单浏览器、单账号、单代理；桥接不可达不得再污染 Web 出口池。
- 生产曾出现宿主机新版 `app.py` 与 `chrome146` 镜像内旧版不一致，导致 UA/CDP 和会话连续性修复没有实际运行。Compose 现将项目目录 `browser-bridge/app.py` 只读挂载到 `/bridge/app.py`，Chrome 镜像仅作为运行时。
- 另一处直接故障是桥接密钥 bind 源缺失后被 Docker 静默创建为目录，主服务和桥接都无法读取密钥，所有请求在 17ms 内返回 401/502。Panda 已恢复 `/root/.secrets/grok2api-browser-bridge-key-main`，三侧 SHA-256 一致；Compose 使用 `create_host_path: false`，缺失密钥时直接拒绝启动。
- 密钥修复后真实 Chromium canary 已进入浏览器启动阶段，但 12.32 秒时 Panda load1 升至 2.10，命中 2 核主机硬门槛并被自动停止。当前 Web 阻塞已从“配置/鉴权错误”收敛为“Panda 无法在资源门槛内承载 Chromium”；应把桥接迁移到独立浏览器 worker，而不是放宽 Panda 门槛。
- 2026-07-16 部署预检发现浏览器桥接被重新启动并占用约 30% CPU / 652.6 MiB，超过其 80% 内存停止线；已先停止桥接，主 API 未中断，停止后可用内存恢复到约 2.2 GiB。后续部署仍禁止隐式拉起桥接。
- Compose 数据目录固定为 `${GROK2API_DATA:-./data}:/app/data`，禁止因 compose 项目名变化切换到空命名卷；Panda 原库已验证完整，包含管理员、账号、出口和模型路由。
- Panda 默认调度改为 Web 启动补偿 0 个、每 30 分钟补偿 1 个；Build 能力探针保持总并发 1，有候选时每 30 秒顺序处理 1 个、扫空后每 5 分钟巡检、启动延迟 2 分钟。关闭的 Build worker 会等待应用退出，不再被 supervisor 当成崩溃循环重启。
- 当前 Webshare 节点不适合继续做全量 Grok 会话尝试；需要可粘滞的住宅/ISP 出口，并让登录与后续请求复用同一 IP、UA 和持久化浏览器 profile。
- 85 个未关联 Web 的 `invalid_grant` Build 账号没有可用的新 RT，无法自动恢复。
- 30 个 Build `access denied` 账号没有已确认的 Build Chat 权限。
- 当前图片尺寸解析仅内置 PNG/JPEG/GIF；若上游保存 WebP，尺寸会暂时显示未知。
- 全局 Web 单并发优先稳定性，会降低高并发吞吐；提升并发前必须在 Panda 上逐级 canary。

## 下一步 3-5 项

1. 继续观察生产探针统计与分池变化，重点跟踪恢复成功率和新 RT 重导入后的自动复活。
2. 更换可粘滞住宅/ISP 出口后做单浏览器 canary；当前机房 Webshare plain HTTP 已证实同样 CF 403。
3. 启动 Grok Web HTTP 逆向 PoC（BE-008），对照 gptimage，目标去掉每请求 Chromium。
4. 观察 Build 恢复池；为 `invalid_grant` 导入新 RT，为 `access denied` 更换具备 Build Chat 权限的授权。
5. 新生图片验证生成耗时、模型和请求分辨率字段（依赖 Web 出口恢复）。

## 与 README 或旧文档的不一致处

- README 描述 Grok Web SSO 不可自动续期；本项目可以保活、同步和重新导入，但不能从已失效 SSO 凭空生成新凭据。
- README 未详细说明 Panda 的生产资源限制，Panda 操作以本维护文档和全局 `panda-remote-ops` 规则为准。
