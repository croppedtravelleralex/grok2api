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

## 更新纪律

- 新事实更新 [02-current-state.md](./02-current-state.md)。
- 优先级变化更新 [03-roadmap.md](./03-roadmap.md)。
- 新风险和技术债更新 [04-improvement-backlog.md](./04-improvement-backlog.md)。
- 每轮工作追加 `logs/YYYY/YYYY-MM.md`；历史日志只追加。
