# 生图流水线调度与时序图 — 部署/验收状态（2026-07-22）

> 对照计划：`生图流水线时序图_4d857836.plan.md`
> 分支：`codex/panda-safe-completion`
> 生产代码头：`613a305`（账号单飞修复 `52c04ec`）
> GHCR（多架构 manifest）：`ghcr.io/croppedtravelleralex/grok2api@sha256:5da879fca7dab2f425e29c9b91468875b04cf9e157f4f848e6d54b2a73015295`
> 仓库：`/opt/grok2api`（compose + `.env` 钉 digest，**无** 源码 git 树）

本文把「计划要做 / 已做 / 未做 / 部署验收」写全，并作为执行待办清单。

---

## 0. 一句话结论

流水线、实时 Timeline、每请求新票、模型级账号降权、同账号 Lite 单飞和 SSE 从 1 上探均已构建并部署。生产已证明纯 HTTP 链路可成功，但 fresh signer 下最终单请求仍被账号 Imagine 额度/soft-stop 阻塞；因此 10 槽能力已上线，**10/10 成功验收未完成**，4/10 档按门禁未运行。

---

## 1. 计划要做的（对照原计划）

| ID | 计划项 | 验收口径 |
|----|--------|----------|
| P1 | 同步 `/v1/images/generations` 协议不变 | OpenAI 兼容 JSON 仍可 200 返回 url |
| P2 | 不复用视频 `mediaQueue` | 独立 `ImagePipelineScheduler` |
| P3 | Trace/Segment 持久化（queue/expand/sse/download） | 表 + Admin API 可读 |
| P4 | 准入排队在选号前 | `Admit` 先于 `selector.Acquire` |
| P5 | 账号 lease SSE 后早释 | 下图前 `EarlyAccountRelease` |
| P6 | 扩写独立闸门 `ScopeWebExpand` / expandGate | 与 Drawing SSE 分闸 |
| P7 | 固定 10 槽 + 队列 100 → 满则 429 | ErrImagePipelineFull |
| P8 | 扩写池 2；SSE AIMD 2–6 + 错峰；下图≈8 | 调度器快照字段 |
| P9 | ready FIFO + aging | 防饿死 |
| P10 | 对话保留位 / 勿压死 chat | webConcurrency 与 expand 分离 |
| P11 | Quality/WS 不进流水线 | 仅 Lite 路径 Admit |
| P12 | Admin timeline API（30m/1h/6h/12h） | `GET /api/admin/v1/image-timeline` |
| P13 | 前端左侧「时序图」甘特 | `/image-timeline` |
| P14 | 单元测试（阶段/队列满/lane/AIMD） | `go test ./internal/application/imagepipeline` |
| P15 | panda canary 2/4/10 + P50/P90 | `GROK2API_GROUPS=2,4,10` |
| P16 | 文档记录最终参数与分位数 | 07 / 月报 / 本文件 |

---

## 2. 已做（代码 / 镜像 / 本地验证）

### 2.1 已合并提交

| Commit | 说明 |
|--------|------|
| `52e04eb` | feat: 生图流水线调度器 + 实时甘特时序图 |
| `8b696c5` | fix: trace 在分槽后再落库，避免 `lane=-1` 写库失败 |
| `a7b708d` | FIFO/Timeline/每请求新 Statsig 票/细分 timing |
| `1a99f25` | 默认 SSE 从单并发安全上探 |
| `7ae4dc1` | typed soft-stop 与换账号重试 |
| `ae76271` | 模型级成功偏好、soft-stop 降权与 Lite 6 次尝试 |
| `52c04ec` | Lite 同账号并发固定为 1，避免成功账号被并发复用 |
| `613a305` | 应用启动统一使用 SSE 默认 `1→1→6`，消除 `2→3→6` 漂移 |

### 2.2 已实现能力映射

| 计划 ID | 状态 | 落点 |
|---------|------|------|
| P1 | Done | handler / gateway 同步返回不变 |
| P2 | Done | `application/imagepipeline`，未碰 video mediaQueue |
| P3 | Done | `image_pipeline_traces` / `image_pipeline_segments` + repo |
| P4 | Done | `executeImage` → `Admit` 再选号 |
| P5 | Done | Lite `generateLiteImage` SSE 后 `EarlyAccountRelease` |
| P6 | Done | `ScopeWebExpand` + Manager `expandGate`；扩写走 `openChatWithScope` |
| P7 | Done | slots=10, queue=100, `ErrImagePipelineFull` → 429 + Retry-After |
| P8 | Done | expand=cfg.ExpandConcurrency(默认2)；SSE min1/init1/max6 stagger400ms；download=assetConcurrency(生产8) |
| P9 | Done（本地） | 准入及 expand/SSE/download 均为显式 FIFO；等待时间单调增加即 aging，旧请求不可被插队 |
| P10 | Partial | expand 与 web 闸门分离；生产 `webConcurrency` 调参仍须 canary |
| P11 | Done | 仅 `OperationImage` Admit；Edit/Quality 不入队 |
| P12 | Done | `transport/http/imagepipeline` |
| P13 | Done | nav + router + `features/image-timeline` |
| P14 | Done | scheduler 单测通过；CI Verify 通过 |
| P15 | Partial / Blocked | 1→2 门禁执行；2 档仍受账号 soft-stop/额度阻塞，按规则未升 4/10 |
| P16 | Done | 本文已回写部署、失败分位数、根因和真实未验收状态 |

### 2.3 本地/CI 证据

- `go test ./...`、`go vet ./...`：通过
- `pnpm lint`、`pnpm build`：通过，含实时槽位/FIFO 队列页面
- `go test -race ./internal/application/imagepipeline`：本机 MinGW `cc1.exe` 不支持 64-bit，属于工具链阻塞；普通并发/取消/容量归还测试通过
- 最终 GHCR：run `29901662897` success；revision=`613a305`；manifest digest=`sha256:5da879fc…15295`
- 本轮后端全量 `go test -count=1 ./...`、`go vet ./...` 通过；CI 同时通过 Swagger、前端 lint/build、amd64/arm64 发布。

### 2.4 部署路径约定（强制）

1. GitHub Actions 构建 GHCR（禁止 panda 上 `docker build`）
2. panda 只改 `/opt/grok2api/.env` 的 `GROK2API_IMAGE=@sha256:…`
3. `docker compose pull && docker compose up -d`（仅 pull/up）
4. 重建容器后重跑 `/tmp/start_signer_nsenter.sh`（签名器 netns）
5. `curl healthz/readyz` 贴真实输出
6. 禁止 scp/rsync 源码或二进制当正式发布

---

## 3. 未做 / 缺口（必须当待办）

| ID | 缺口 | 优先级 | 说明 |
|----|------|--------|------|
| G1 | **构建并部署本轮新镜像** | Done | 生产为 `5da879fc…15295` / `613a305` |
| G2 | **1/2/4/10 并发 canary + 记录 P50/P90/成功率** | Blocked | fresh signer 下最终单档连续 429；4/10 未运行，需先补足可生图账号 |
| G3 | **时序图 API/页面生产验收** | Partial | Admin API 已确认 traces/snapshot；页面人工视觉验收仍待做 |
| G4 | ready FIFO + **aging** | P1 | **本地完成**：四类队列显式 FIFO，取消不泄槽/容量，旧请求不可被新请求越过 |
| G5 | `generationTiming` 细字段日志 | P2 | **本地完成**：新增 pipeline_queue/expand/sse/download_ms |
| G6 | Settings UI/API 暴露 `expandConcurrency` | P2 | **本地完成**：API/持久化/UI/restartRequired 已贯通并有后端测试 |
| G7 | 对话保留位显式预留（webConcurrency→8） | P1 | 当前单出口保持 webConcurrency=2；不要在账号接受率未恢复前上调 |
| G8 | Quality/WS 纳入流水线 | P3 | 计划明确先不做 |
| G9 | 前端单测（空数据/失败边框/窄屏） | P2 | 尚无前端测试框架；lint、TypeScript 与生产构建通过，专用交互测试仍待补 |
| G10 | soft_stop AIMD 生产观察调参 | P1 | 代码会 MarkSoftStop；需 canary 后决定 SSE max |
| G11 | NewAPI `#105` 是否重开走流水线 e2e | P2 | 当前渠道策略仍可能关闭；直连 grok2api canary 即可验收 |
| G12 | 仓库 `deploy/panda/docker-compose.yml` digest 与生产 `.env` 同步 | P2 | 生产 `.env` 已钉 `5da879fc…`；仓库默认仍是旧安全基线，待不干扰现有工作树时单独更新 |

---

## 4. 待办事项清单（执行用）

### 部署与验收（本轮必须做完）

- [x] **D1** CI 构建本轮新镜像；记录 manifest digest；备份 panda `.env` 后写入新 digest
- [x] **D2** `cd /opt/grok2api && docker compose pull && docker compose up -d`
- [x] **D3** 重拉签名器并同步 fresh browser matched pair；signer health 200
- [x] **D4** `healthz={"ok":true}`；`readyz.ready=true`（Statsig 预热为 degraded，按需刷新）
- [x] **D5** 容器 Image digest = `5da879fc…15295`，revision 对应 `613a305`
- [ ] **D6** canary 已执行 1→2；最终单请求连续 429，按门禁停止 2/4/10
- [x] **D7** canary JSON 摘要已写入本文 §5
- [x] **D8** Admin Timeline：84+ traces，snapshot 参数完整
- [ ] **D9** 浏览器打开 `https://grokimage.relai.asia/image-timeline`（或本机反代）确认甘特刷新

### 功能补齐（可下一迭代）

- [x] **F1** 显式 FIFO/aging 阶段队列（越早入队优先级越高）— 对应 G4
- [x] **F2** settings 透出 `expandConcurrency` + restartRequired — G6
- [ ] **F3** 评估生产 `webConcurrency` 提到 6–8，避免 SSE 池被出口闸门饿死 — G7
- [x] **F4** 细化 `generation_timing` 日志字段（queue/expand/sse/download ms）— G5
- [ ] **F5** 前端 timeline 基础测试 — G9
- [x] **F6** 同步 `deploy/panda` 默认 digest，并让 bridge 默认关闭 — G12

### 明确不做（记录以免误开）

- [ ] ~~Quality/WS 进同步流水线~~ — 计划排除（G8）
- [ ] ~~文生图塞进视频 mediaQueue~~ — 禁止
- [ ] ~~panda 上 docker build / scp 热更~~ — 禁止

---

## 5. 生产实测

| 项 | 值 |
|----|-----|
| 部署时间 (UTC+8) | 2026-07-22 15:56（最终镜像） |
| 镜像 digest | `sha256:5da879fca7dab2f425e29c9b91468875b04cf9e157f4f848e6d54b2a73015295` |
| healthz | `{"ok":true}`；容器 healthy |
| readyz | `ready=true`，Statsig startup 预热 degraded、请求按需刷新 |
| 签名器 | 当前模块索引 `[38,33,24,32]`；一次性 Chrome 捕获 fresh matched pair；请求期纯 HTTP |
| 快照 | slots=10、queue=100、expand=2、SSE target=1/max=6、download=8 |
| 单请求成功样本 | 1/1，17.92s（账号状态预热后） |
| signer 抽钥本机闭环 | text 200/0.88s；Lite 200/5.74s（Chrome 已关闭后的纯 HTTP） |
| 最终单请求 | 0/1，429/27.04s；再次 0/1，429/34.97s |
| 修复前 2 并发 | 1/2，wall 11.76s；同账号复用导致一路 `too many requests in progress` |
| 修复后 2 并发诊断 | 0/2，wall 61.73s，P50 42.41s/P90 57.87s；不同账号，一路 6 次 soft-stop、一路真实 429 |
| canary 4 / 10 | 未运行：上一级门禁未通过 |
| timeline API | 通过；traces/snapshot 可读，SSE target 已确认 1 |
| 最终建议参数 | web=2、expand=2、asset/download=8、SSE=1→6 AIMD；先修账号池再升并发 |

旧镜像失败基线（2026-07-22）：80/80 `403 upstream_error`，P50≈15.97s、P90≈27.53s。该数据仅用于证明旧实现未通过生产验收，不计为本轮新镜像结果。

验收目标（计划）：

- 2 并发：正常样本约 8–20s，无明显额外排队
- 10 并发：成功率 ≥95%，P90 尽量 ≤45s；当前未达到，不得把“10 槽”写成“10/10 成功”

### 5.1 429/soft-stop 根因链

- 单请求也会 soft-stop 或 `usage_limit_reached`，因此“两个账号同时才触发”被证伪。
- 修复前的两并发额外暴露同账号被成功偏好重复选择；`52c04ec` 已将 Lite 同账号并发压到 1，复验两路使用不同账号。
- fresh signer 后仍有多个不同账号 soft-stop/额度耗尽，说明当前瓶颈是 Imagine 可用账号数量与上游接受率，而非本地 queue、下载带宽、签名 403或账号 lease 泄漏。
- 生产 20 个 dispatch 账号不足以证明有 10 个可同时生图账号；必须先逐号建立模型级成功证据。

---

## 6. 关联文件

| 角色 | 路径 |
|------|------|
| 调度器 | `backend/internal/application/imagepipeline/` |
| Gateway | `backend/internal/application/gateway/service.go` |
| Lite/扩写 | `backend/internal/infra/provider/web/image.go` |
| Egress | `backend/internal/infra/egress/manager.go` |
| Admin API | `backend/internal/transport/http/imagepipeline/handler.go` |
| 前端 | `frontend/src/features/image-timeline/` |
| Canary | `tools/panda_image_conc_canary.py` |
| 运维事实 | `docs/07-udeal-zero-browser-ops-2026-07-21.md` |

---

## 7. 变更日志

| 时间 | 事件 |
|------|------|
| 2026-07-21 | 代码合入 `52e04eb` / `8b696c5`，GHCR 构建成功 |
| 2026-07-22 | 本文建立；确认 panda 仍为 `9e80643e…`，开始升 digest 部署与 canary |
| 2026-07-22 | 部署 `ae76271` 后定位模型级 quota/soft-stop；部署 `52c04ec` 修复同账号并发复用 |
| 2026-07-22 | 部署 `613a305`，生产 SSE 起点修正为 1；fresh signer 消除 403，但最终单档 429，停止 2/4/10 |
