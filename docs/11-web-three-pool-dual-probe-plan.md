# Web 三池 + 双探针方案（双轨粒度）

> 落地档：对应集成轨 `codex/panda-safe-completion`。回滚 tag：`rollback/web-pool-baseline` / `rollback/stage0-complete` @ `16511d9`。

## 决策锁定

| 项 | 选择 |
|---|---|
| 池粒度 | **双轨**：图调度轨 `image` + 聊调度轨 `chat`，同账号可有不同池位 |
| 死池复活 | **允许极低频真实探测**（Lite/Chat），参考 Build 维护探针 dead 车道 |

## 三池谓词（每账号 × 每轨）

判定顺序：`dead` → `enabled/active` → `recovery` → `dispatch`。

- **dead**：`lastError` 以 `web_dead:` 开头；聊轨 `auth_failed`；图轨持久 `signature_failed`。不物理删除。
- **recovery**：冷却、额度耗尽、无证据 unknown、model block 到期等；子车道 `recovery_verify` / `recovery_cooldown`（DRR 内 priority，非第四池）。
- **dispatch**：Selector 仅从此池按索引取号。

## 双探针

| 探针 | 真实业务请求 | 职责 |
|---|---|---|
| 调度探针 | **零** Lite/Chat | L0 token + 只读 quota 同步，更新 DispatchIndex |
| 维护探针 | L0/L1/L2 受预算约束 | DRR recovery:dead ≈ 7:3（verify 非空时 5:3:2） |

**Probe Budget Governor**：流水线占用 ≥7 暂停 L2；≥9 维护探针仅 L0。生产 `ImagePipeline.Admit` 优先。

环境变量（复用 Build 命名风格）：

- `GROK2API_WEB_PROBE_EVERY=30s`
- `GROK2API_WEB_PROBE_IDLE_EVERY=5m`
- `WEB_PROBE_LITE_MAX_PER_ACCOUNT_PER_DAY=1`
- `WEB_PROBE_CHAT_MAX_PER_ACCOUNT_PER_DAY=3`
- `WEB_PROBE_LITE_GLOBAL_PER_HOUR=6`

## 代码落点

| 模块 | 文件 |
|---|---|
| 谓词与索引 | `web_pool_probe.go`, `poolindex/web_drr.go` |
| 探针引擎 | `web_probe_monitor.go`, `web_probe_budget.go`, `web_probe_requests.go` |
| Selector | `selector.go` → `loadWebCandidatesByIndex` |
| 启动 | `startup.go` → `runWebDispatchProbe` / `runWebMaintenanceProbe` |
| API/面板 | `GET/PATCH /accounts/web-probe`, `web-probe-panel.tsx` |

## 验收清单 V1–V7

| # | 条件 |
|---|---|
| V1 | 调度探针零 Lite/Chat 上游请求 |
| V2 | 维护探针 recovery:dead ≈ 7:3 (±15%) |
| V3 | `web_dead:` 后 DB 行仍在；低频 L2 可写 modelState |
| V4 | 同账号图=dispatch + 聊=recovery 可并存 |
| V5 | pipeline 10/10 时探针 L2 降为 0 |
| V6 | Web Acquire 不扫全表 |
| V7 | 外挂 probe 不再对 Web 重复维护 refresh |

## 参考

- Build 四池：`four_pool_probe.go`, `docs/08-build-four-pool-dual-probe-todos-2026-07-22.md`
- Imagine 额度：`docs/09-imagine-quota-model-state-and-10-concurrency-todos-2026-07-22.md`
