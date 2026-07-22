# 生图流水线调度与时序图 — 部署/验收状态（2026-07-22）

> 对照计划：`生图流水线时序图_4d857836.plan.md`
> 分支：`codex/panda-safe-completion`
> 代码头：本轮本地工作树（基于 `8b696c5`，尚未构建发布）
> GHCR（多架构 manifest）：`ghcr.io/croppedtravelleralex/grok2api@sha256:4764cf41257a10b8f10599c26d661e2e3a366a73be4beb44057b266dcad79ece`
> 仓库：`/opt/grok2api`（compose + `.env` 钉 digest，**无** 源码 git 树）

本文把「计划要做 / 已做 / 未做 / 部署验收」写全，并作为执行待办清单。

---

## 0. 一句话结论

本地已补齐显式 FIFO/aging 调度、实时槽位与阶段队列、trace 落库修复、每请求新签名票、`expandConcurrency` 设置和分阶段 timing。Panda 已运行 digest `4764cf41…`，但该镜像之前的 80 次生产请求为 **80/80 上游 403**；本轮修复尚未构建发布，不能把“纯 HTTP 已上线”写成“生产已验收”。

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
| P8 | Done（参数写死/配置） | expand=cfg.ExpandConcurrency(默认2)；SSE min2/init3/max6 stagger400ms；download=assetConcurrency |
| P9 | Done（本地） | 准入及 expand/SSE/download 均为显式 FIFO；等待时间单调增加即 aging，旧请求不可被插队 |
| P10 | Partial | expand 与 web 闸门分离；生产 `webConcurrency` 调参仍须 canary |
| P11 | Done | 仅 `OperationImage` Admit；Edit/Quality 不入队 |
| P12 | Done | `transport/http/imagepipeline` |
| P13 | Done | nav + router + `features/image-timeline` |
| P14 | Done | scheduler 单测通过；CI Verify 通过 |
| P16 | Partial | 本文已回写本地证据与失败基线；**成功分位数待新镜像 canary 后补** |

### 2.3 本地/CI 证据

- `go test ./...`、`go vet ./...`：通过
- `pnpm lint`、`pnpm build`：通过，含实时槽位/FIFO 队列页面
- `go test -race ./internal/application/imagepipeline`：本机 MinGW `cc1.exe` 不支持 64-bit，属于工具链阻塞；普通并发/取消/容量归还测试通过
- GHCR：run `29846799996` success；revision=`8b696c5`；manifest digest=`sha256:4764cf41…`

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
| G1 | **构建并部署本轮新镜像** | P0 | 当前生产 `4764cf41…` 不含本轮修复；部署属于 Panda 安全门槛 |
| G2 | **1/2/4/10 并发 canary + 记录 P50/P90/成功率** | P0 | 旧镜像 80 次为 80/80 403；新镜像必须从单请求重新开始 |
| G3 | **时序图 API/页面生产验收** | P0 | Admin JWT 调 `image-timeline`；浏览器开 `/image-timeline` |
| G4 | ready FIFO + **aging** | P1 | **本地完成**：四类队列显式 FIFO，取消不泄槽/容量，旧请求不可被新请求越过 |
| G5 | `generationTiming` 细字段日志 | P2 | **本地完成**：新增 pipeline_queue/expand/sse/download_ms |
| G6 | Settings UI/API 暴露 `expandConcurrency` | P2 | **本地完成**：API/持久化/UI/restartRequired 已贯通并有后端测试 |
| G7 | 对话保留位显式预留（webConcurrency→8） | P1 | 计划「SSE max+对话保留」；生产仍常 `webConcurrency=2`，SSE 池易被 webGate 卡住 |
| G8 | Quality/WS 纳入流水线 | P3 | 计划明确先不做 |
| G9 | 前端单测（空数据/失败边框/窄屏） | P2 | 尚无前端测试框架；lint、TypeScript 与生产构建通过，专用交互测试仍待补 |
| G10 | soft_stop AIMD 生产观察调参 | P1 | 代码会 MarkSoftStop；需 canary 后决定 SSE max |
| G11 | NewAPI `#105` 是否重开走流水线 e2e | P2 | 当前渠道策略仍可能关闭；直连 grok2api canary 即可验收 |
| G12 | 仓库 `deploy/panda/docker-compose.yml` digest 与生产 `.env` 同步 | P2 | **本地完成**：默认 digest 同步到 `4764cf41…`，bridge 默认关闭；新镜像发布后再更新 digest |

---

## 4. 待办事项清单（执行用）

### 部署与验收（本轮必须做完）

- [ ] **D1** CI 构建本轮新镜像；记录 manifest digest；备份 panda `.env` 后写入新 digest
- [ ] **D2** `cd /opt/grok2api && docker compose pull && docker compose up -d`
- [ ] **D3** 重拉签名器：`/tmp/start_signer_nsenter.sh`（或等价 nsenter 脚本）
- [ ] **D4** 健康检查：`curl -sS http://127.0.0.1:18000/healthz` 与 `/readyz`（贴输出）
- [ ] **D5** 确认容器 Image digest = `4764cf41…`，revision 对应 `8b696c5`
- [ ] **D6** canary：严格按 `1→2→4→10` 分阶执行；每阶检查 CPU/内存/load/SSH/health/403，再决定是否升级
- [ ] **D7** 把 canary JSON（success/P50/P90）写入本文件 §5
- [ ] **D8** Admin：`GET /api/admin/v1/image-timeline?window=30m` 有 traces/snapshot
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

## 5. 生产实测（部署后填写）

| 项 | 值 |
|----|-----|
| 部署时间 (UTC+8) | _待填_ |
| 镜像 digest | _待填_ |
| healthz | _待填_ |
| readyz | _待填_ |
| 签名器 | _待填_ |
| canary 2：success / P50 / P90 | _待填_ |
| canary 4：success / P50 / P90 | _待填_ |
| canary 10：success / P50 / P90 | _待填_ |
| timeline API | _待填_ |
| 最终建议参数（web/expand/asset/SSE） | _待填_ |

旧镜像失败基线（2026-07-22）：80/80 `403 upstream_error`，P50≈15.97s、P90≈27.53s。该数据仅用于证明旧实现未通过生产验收，不计为本轮新镜像结果。

验收目标（计划）：

- 2 并发：正常样本约 8–20s，无明显额外排队
- 10 并发：成功率 ≥95%，P90 尽量 ≤45s；若 soft_stop 主导则下调 SSE max，不靠抬下图并发掩盖

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
