# grokImage 当前状态主档

## 最后更新时间

- 日期：2026-07-15
- 维护目的：记录图片管理增强、Web 并发治理和 Build 异常账号处理的真实状态。

## 整体状态摘要

- 后端为 Go 网关，前端为 React/Vite 管理端，支持 Grok Build、Web、Console 三个账号池。
- 本地工作分支为 `codex/panda-safe-completion`；Panda 当前运行上一版镜像，本轮改动尚待 CI 镜像部署。
- Panda 为低资源生产机，部署采用本地提交、GitHub Actions/GHCR 构建、Panda 拉取镜像的方式。
- NewAPI 已按 Chat Completions、Responses、Messages、Images 和 Videos 拆分接入；本轮不改其渠道结构。

## 已完成功能

### 基础架构

- SQLite/PostgreSQL、内存/Redis 运行时、账号池、模型路由、请求审计和代理出口。
- Panda 域名入口为 `grokimage.relai.asia`，生产服务由容器运行。
- Web 与 Web Asset 使用分离的出口作用域；本轮新增各自全局单并发闸门，避免 Webshare/浏览器链路被并发打满，同时避免 WebSocket 与图片下载互相死锁。

### 核心业务能力

- 支持 Responses、Chat Completions、Anthropic Messages、图片生成/编辑和异步视频接口。
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

- Panda 上 Build 启用账号：88 个 `active`，107 个 `reauthRequired`。
- 107 个异常账号中：87 个为 OAuth `invalid_grant`，表示 Refresh Token 已撤销或轮换；20 个为 Build Chat `access denied`，表示当前账号没有对应 Build 能力。
- 87 个 `invalid_grant` 中只有 2 个与现有 Web 账号关联，可在 Web SSO 有效时通过 Web→Build 转换尝试修复；其余必须导入新的有效 RT。
- 20 个 `access denied` 账号继续隔离在调度池外；刷新同一个 Token 不会产生 Build 权限。
- Web 额度刷新应用层原本已有独立单并发池；Panda 单节点代理测试显示 Webshare 连接正常，但直接访问 Grok 返回 Cloudflare 403，因此失败根因不只是并发，还包括浏览器/Cloudflare 会话状态。

## 进行中事项

- 等待本轮代码提交、CI 镜像构建和 Panda 低资源部署。
- 部署后只做一个 Web 账号、一个请求的浏览器链路 canary，不运行全量刷新。
- 若 Web canary 成功，再尝试修复 2 个已有 Web 关联的 Build 异常账号。

## 已知阻塞与风险

- Grok Web 仍可能因 Cloudflare 会话、SSO 失效或浏览器桥接状态返回 403；代理白名单正常不能证明上游授权正常。
- 85 个未关联 Web 的 `invalid_grant` Build 账号没有可用的新 RT，无法自动恢复。
- 20 个 Build `access denied` 账号没有已确认的 Build Chat 权限。
- 当前图片尺寸解析仅内置 PNG/JPEG/GIF；若上游保存 WebP，尺寸会暂时显示未知。
- 全局 Web 单并发优先稳定性，会降低高并发吞吐；提升并发前必须在 Panda 上逐级 canary。

## 下一步 3-5 项

1. 提交并推送本轮改动，等待 GHCR 镜像完成。
2. Panda 容量预检后原子更新一个主服务容器并验证健康、内存和负载。
3. 验证图片页面、日期删除 API 和新图片元数据。
4. 启动最多一个受限浏览器会话，验证单 Web 账号额度或模型请求。
5. Web 链路通过后仅修复 2 个可关联恢复的 Build 账号；其余等待新 RT 或有效权限账号。

## 与 README 或旧文档的不一致处

- README 描述 Grok Web SSO 不可自动续期；本项目可以保活、同步和重新导入，但不能从已失效 SSO 凭空生成新凭据。
- README 未详细说明 Panda 的生产资源限制，Panda 操作以本维护文档和全局 `panda-remote-ops` 规则为准。
