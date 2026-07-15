# grokImage 当前状态主档

## 最后更新时间

- 日期：2026-07-15
- 维护目的：记录 Messages 协议修复、Web 出口诊断和 Build 异常账号治理的真实状态。

## 整体状态摘要

- 后端为 Go 网关，前端为 React/Vite 管理端，支持 Grok Build、Web、Console 三个账号池。
- 本地工作分支为 `codex/panda-safe-completion`；Panda 已运行本轮镜像摘要 `sha256:80d6ab8d5922...`。
- Panda 为低资源生产机，部署采用本地提交、GitHub Actions/GHCR 构建、Panda 拉取镜像的方式。
- NewAPI 已按 Chat Completions、Responses、Messages、Images 和 Videos 拆分接入；本轮不改其渠道结构。

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
- 图片本地归档已记录请求 ID、模型、请求分辨率、实际宽高、生成耗时和精确到秒的时间。
- 图片管理已支持本地日期筛选、按日期分组、按日期删除和一键删除全部。
- 旧图片在读取时会从 PNG/JPEG/GIF 文件解析实际分辨率；旧图片无法反推出历史生成耗时，会显示未知。
- Web→Build 转换在已关联 Build 账号处于 `reauthRequired` 时会更新原账号凭据，而不是错误地跳过。

### 维护性与工程能力

- 后端 `go test ./...` 已通过。
- 前端 `pnpm lint` 与 `pnpm build` 已通过。
- 本地临时实例已完成数据库迁移和管理 API 验收：图片列表、日期筛选、图片读取、元数据展示和按日期删除均通过。
- 已建立 `docs/` 维护入口和 Panda 低资源操作规则。

## 账号池诊断事实

- 完成能力探测后，Panda 上 Build 账号为 80 个 `active`、115 个 `reauthRequired`；195 个账号均保持启用，但异常账号不会进入调度池。
- 当前 115 个异常账号中：85 个为 OAuth `invalid_grant`，30 个为 Build Chat `access denied`。
- 2 个已有 Web 关联的 `invalid_grant` Build 账号已通过 Web→Build 转换换取并保存新凭据，账号 ID 与关联关系得到保留；随后能力探测确认两者仍缺少 Build Chat 权限，因此没有将其伪装为可用账号。
- 剩余 85 个 `invalid_grant` 必须导入新的有效 RT；30 个 `access denied` 账号继续隔离在调度池外，同 Token 刷新不会产生 Build 权限。
- 已修复 Token 刷新无条件把 `access denied` 账号恢复为 active 的缺陷；权限拒绝后不再强制刷新 RT。
- Build 在线请求优先且仅使用已有成功响应型号记录的账号；未验证账号不再借真实用户请求试错。诊断快照中 active 账号只有少量已有成功响应记录，因此后续必须使用单并发能力探测逐步扩大可信池。
- Panda 的 Build 能力探测配置为延迟 2 分钟启动、每 5 分钟只处理 1 个未验证账号；成功才进入可信池，权限拒绝转为 `reauthRequired`，临时错误冷却 15 分钟。
- 重新导入处于 `reauthRequired` 的 Build 账号时会清除旧 `observed_model`，防止旧权限结论污染新凭据；新凭据必须重新通过能力探测。
- Web 额度刷新应用层原本已有独立单并发池；Panda 单节点代理测试显示 Webshare 连接正常，但直接访问 Grok 返回 Cloudflare 403。
- 单浏览器、单账号额度 canary 在加载 Grok 页面阶段超时并返回 502；桥接容器已停止，未执行全量刷新。当前根因是所选代理上的 Cloudflare 浏览器会话无法建立，不是代理白名单或并发连接失败。
- Webshare 100 个出口仅分布在 10 个 `/24`，集中于新加坡机房 ASN。Panda canary 已证明代理鉴权和外网连通正常、但 Grok 返回 Cloudflare 403；主要风险是机房 ASN 信誉、IP/账号地域不一致，以及 SSO/clearance 与原始 IP、UA、TLS/浏览器指纹不一致。

## 进行中事项

- 浏览器桥接的 30 秒启动上限、阶段化错误和结构化 502 已部署；桥接默认保持停止，避免一次失败长期占用 Panda。
- 等待新的住宅/ISP 出口或可建立 Cloudflare 会话的节点后，再做一个 Web 账号 canary。

## 已知阻塞与风险

- Grok Web 当前被 Cloudflare 浏览器会话建立失败阻塞；代理白名单正常不能证明该出口可通过 Grok 风控。
- 当前 Webshare 节点不适合继续做全量 Grok 会话尝试；需要可粘滞的住宅/ISP 出口，并让登录与后续请求复用同一 IP、UA 和持久化浏览器 profile。
- 85 个未关联 Web 的 `invalid_grant` Build 账号没有可用的新 RT，无法自动恢复。
- 30 个 Build `access denied` 账号没有已确认的 Build Chat 权限。
- 当前图片尺寸解析仅内置 PNG/JPEG/GIF；若上游保存 WebP，尺寸会暂时显示未知。
- 全局 Web 单并发优先稳定性，会降低高并发吞吐；提升并发前必须在 Panda 上逐级 canary。

## 下一步 3-5 项

1. 保持浏览器桥接停止，避免无效会话占用内存。
2. 更换可通过 Grok Cloudflare 的住宅/ISP 代理后进行单账号 canary。
3. 为 `invalid_grant` 账号导入新 RT；为 `access denied` 账号更换具备 Build Chat 权限的授权，后台会以单并发最小请求验证后再放入可信池。
4. 新生图片验证生成耗时、模型和请求分辨率字段；旧图片只展示可解析的实际尺寸。

## 与 README 或旧文档的不一致处

- README 描述 Grok Web SSO 不可自动续期；本项目可以保活、同步和重新导入，但不能从已失效 SSO 凭空生成新凭据。
- README 未详细说明 Panda 的生产资源限制，Panda 操作以本维护文档和全局 `panda-remote-ops` 规则为准。
