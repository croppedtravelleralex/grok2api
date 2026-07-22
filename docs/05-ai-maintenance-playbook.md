# grokImage AI 维护接手手册

## AI 接手阅读顺序

1. 读 [README.md](./README.md) 和 [02-current-state.md](./02-current-state.md)。
2. 涉及范围和边界时读 [01-project-charter.md](./01-project-charter.md)。
3. 涉及上线顺序时读 [03-roadmap.md](./03-roadmap.md)。
4. 涉及技术债时读 [04-improvement-backlog.md](./04-improvement-backlog.md)。
5. 读当月日志，再进入相关代码、配置、数据库和命令验证。

## 本地默认流程

1. 在 Windows 本机读代码、改代码、跑测试、构建前端和准备镜像提交。
2. 后端至少执行受影响包测试；收尾前执行 `cd backend; go test ./...`。
3. 前端至少执行 `cd frontend; pnpm lint; pnpm build`。
4. 删除/导入/修复账号前先分类错误，不对 `invalid_grant` 做无意义重试。
5. 不把 Token、Cookie、密码、代理口令写入日志、提交或维护文档。

## Panda 强制规则

- 所有 Panda 操作必须使用 `panda-remote-ops` 流程。
- **禁止在 Panda 上编译或构建**：不得 `go build` / 全量 `go test`、`docker build`、`pnpm build`、装构建依赖或生成镜像。Panda 只拉取并运行已构建产物。
- 标准部署链：Windows 本机改代码并跑通测试 → push 到 GitHub（触发 Actions/GHCR）→ Panda `pull` 镜像并重建容器 → 按需清理 GHCR/临时产物；不在 Panda 留构建缓存。
- 任何变更前报告：内存、负载/CPU、磁盘、服务健康、预算、canary、停止线和回滚。
- Panda 不构建镜像；通过 GitHub Actions/GHCR 拉取本地已验证的产物。
- 浏览器最多先启一个会话；账号刷新、额度同步和模型测试从并发 1 开始。
- 不同时运行启动追赶、全量刷新、浏览器测试和多接口回归。
- 可用内存低于 1 GiB、归一化 load1 高于 0.70、磁盘高于 85%或 SSH 变慢时不启动变更。
- 可用内存低于 768 MiB、归一化 load1 高于 1.0、主服务不健康或出现 OOM/Swap 增长时立即停止最新负载并回滚。

## 账号问题判断

- Build `invalid_grant`：RT 已撤销/轮换；只有导入新 RT，或用有效且已关联的 Web SSO 重新生成 Build 凭据。
- Build `access denied`：账号缺少 Build Chat 能力；同 Token 重刷无效，保持隔离并换有权限账号。
- Build 自动恢复：同一个单并发能力 worker 交替处理恢复池和待验证池；不可恢复账号只软退役，禁止后台直接级联硬删。重新导入新凭据应保留账号 ID 并复活。
- Web 403：先区分代理连接、Cloudflare 浏览器状态和 SSO 是否有效；代理 IP 白名单成功不等于 Grok 会话有效。
- Web 浏览器身份：代理、SSO/clearance、UA、平台和浏览器 profile 必须保持一致；桥接停止通常是失败后的保护结果，不能当作 403 根因。
- Web 全量额度刷新：必须走独立单并发池，先单账号 canary，禁止直接全量重跑。

## 纯 HTTP Lite 生图运维

- 范围只包括 `grok-imagine-image` Lite 文生图；Quality/WS、Edit 和 Video 不得借“10 并发”名义一起开启。
- 10 并发口径是 10 个客户端请求进入 10 个 pipeline slots。生产基线为 queue=100、Expand=2、SSE AIMD `1→6`、Download=8；单出口不得直接硬开 10 条 SSE。
- 同账号 Lite 生图并发必须为 1。若日志出现 `You have too many requests in progress`，先查两条 trace 是否拿到同一 account ID，不要误判为账号额度 429。
- 失败分类：`ErrImagePipelineFull` 是本地 429；`usage_limit_reached` 是上游真实 429；`isSoftStop=true` 是无图终止，常映射为 502。三者的处理和指标必须分开。
- soft-stop 只做模型级降权，不污染账号全局健康或其他模型；成功偏好、未知、soft-stop 的排序也只作用于当前模型。

部署/重建顺序：

1. 运行 Panda preflight，备份 `/opt/grok2api/.env`，只通过 GHCR digest pull/up 主服务。
2. 重拉 `/tmp/start_signer_nsenter.sh`；不要在包含 `zb_local_signer.py` 字样的 SSH 父命令里执行其内部 `pkill -f`，避免匹配父进程自杀。
3. 确认 signer module/x-values。2026-07-22 当前值为 `[38,33,24,32]`，旧值 `[31,16,43,8]` 不可继续使用。
4. signer `/healthz` 200 只证明进程存活。必须先用一个真实 `/rest/modes` 或 Lite 请求验证 code 7；所有账号 4–6 秒 403 时优先查 matched seed+HEX，不要刷新全账号池。
5. 动态 challenge 生成的 pair 已出现“本地 refresh_ok、上游 code 7”；当前临时使用一次性 Chrome 捕获的真实 digest pair，抽钥后请求期仍为纯 HTTP。不得把 seed、HEX、SSO、Cookie 写入日志或文档。
6. Canary 严格 `1→2→4→10`，每档前重新 preflight。上一级成功率未达标或出现健康/资源停止线，立即停止升档。

每档至少记录 success/total、wall、P50/P90/max、HTTP 状态、soft-stop/429 数、trace 匿名账号数量、queue/expand/SSE/download timing、SSE target、容器资源和 health。不得把“10 槽已部署”描述为“10/10 已成功”。

## 更新纪律

- 新事实更新 [02-current-state.md](./02-current-state.md)。
- 优先级变化更新 [03-roadmap.md](./03-roadmap.md)。
- 新风险和技术债更新 [04-improvement-backlog.md](./04-improvement-backlog.md)。
- 每轮工作追加 `logs/YYYY/YYYY-MM.md`；历史日志只追加。
