# Build 四池 + 双探针：全量盘点与待办（2026-07-22）

> 对应计划：`build池状态机优化_44771e70.plan.md`（**勿改计划文件本身**）。  
> 本文件是该方案的**执行真相档**：已做 / 部分做 / 未做 / 后续待办。  
> 生产快照时间：2026-07-22 上午（panda `build-probe` API）。

---

## 1. 方案目标（计划锁定，全部仍有效）

| # | 产品决策 | 含义 |
| --- | --- | --- |
| 1 | 退役直接删除 | 无 `retired:` 软退役；删除池 → 维护探针物理 `DELETE` |
| 2 | 取消恢复池 | 无长期 `reauthRequired` 驻留 / `recoverBuildChat` 恢复轨 |
| 3 | 保留验证池 | 无 `observed_model` 只在此池做能力探测 |
| 4 | 仅四池 | 调度 / 普通 / 验证 / 删除 |
| 5 | 双探针 | A=调度池；B=验证+普通+删除（DRR） |
| 6 | 可继续优化调度 | 时间轮、Redis ZSET 镜像、覆盖索引等属增强 |

### 验收标准（计划原文）对照

| 验收项 | 状态 | 证据 / 缺口 |
| --- | --- | --- |
| 面板/API 仅四池；无 recovery/retired 驻留计数 | **Done** | `build-probe` 返回 `dispatch/normal/verification/delete`；面板 i18n 已四池化 |
| 选号热路径不扫全表；索引与 DB 对账一致 | **Partial** | 有 `DispatchIndex` 重排；仍先 `ListRoutingCandidates` 全量加载；无正式对账任务 |
| 维护探针大样本占比近似 5:3:2（验证空≈7:3） | **Partial** | DRR 代码+单测有；生产未导出 lane 占比观测 |
| 额度用尽→普通→恢复→调度闭环 | **Done（行为）** | 生产 `normalOk=47`、`normal=2`；逻辑在 `runNormalProbe` |
| `invalid_grant` / `access_denied` ≤1～2 周期内物理删除 | **Done（行为）** | 终态进 `deletable:`；维护探针物理删；启动迁移 backlog |
| 双探针并发 ≤2；Panda 资源无明显抬升 | **Done** | 两独立循环各单并发；健康时约数十 MiB 级 |

---

## 2. 生产现状快照（2026-07-22）

```text
pools:  delete=0  dispatch=211  normal=2  verification=0
purgeApply: true
statistics (进程累计，重启会清零):
  attempts=1138  succeeded≈1023  deleted=53  deletable=1
  dispatchOk=976  normalOk=47  cooledDown=61  failed=115
recent: 以 (dispatch, dispatchOk) 为主
外挂 probe: 跳过 Build（Web/Console only）
容器: healthy
```

解读：

- 删除池 backlog **已清到 0**（上线初约 227，维护探针持续物理删除）。
- 调度池稳定在 ~210；普通池极薄；验证池空。
- 进程统计 `deleted=53` 小于历史 backlog，因**服务重启后计数从零**；池数量以 DB 为准。

---

## 3. 计划 To-do 对照（原 9 项）

| 原 To-do ID | 内容 | 状态 | 说明 |
| --- | --- | --- | --- |
| `four-pool-model` | 四池谓词与路由；废除恢复轨与软退役 | **Done** | `AccountPoolAt`、`four_pool_probe.go`；Build `MarkReauthRequired`→`markBuildDeletable` |
| `ds-indexes` | DispatchIndex + 三路堆 + DRR；SQLite 覆盖索引 | **Partial** | 内存索引/堆/DRR **Done**；计划中的覆盖索引 / Redis ZSET 镜像 **未做** |
| `dual-probe` | 调度探针 + 维护探针 DRR | **Done** | `DispatchProbeTick` / `MaintenanceProbeTick`；startup 双循环 |
| `delete-immediate` | 删除池命中即物理删 | **Done** | `runDeleteProbe` + `purgeApply` 默认 true |
| `quota-transfer` | 额度用尽↔调度；不可恢复→删除 | **Done** | 普通池探针清 recovery / 终态删 |
| `pool-stats-ui` | API/面板/i18n 仅四池 | **Done** | handler + `build-probe-panel` + i18n |
| `migrate-backlog` | `reauthRequired`/`retired:` → `deletable:` | **Done** | 启动 `MigrateBuildDeadAccountsToDeletePool`；生产删除池曾达 227 后清零 |
| `external-probe` | 收窄 panda 外挂 pool-probe | **Done** | `tools/grok2api-account-pool-probe.py` 跳过 Build；主机已更新 |
| `verify-deploy` | 单测→GitHub→panda pull 观察 | **Done** | commit `e373afc` 起；后续镜像续跑；见上快照 |

---

## 4. 已完成清单（按改动面）

### 4.1 产品语义

- [x] 四池互斥谓词：`dispatch` / `normal` / `verification` / `delete`
- [x] 验证池不进真实流量（Selector 跳过无 `observed_model` 的 Build）
- [x] 删除池物理删除（无 retired 中间态作为正式路径）
- [x] 取消 Build 长期恢复队列（无 3 次恢复退避）
- [x] 终态错误进删除：`invalid_grant` / `access_denied` / `permission-denied` 等
- [x] 临时错误短冷却、留当前池
- [x] 启动迁移旧 `reauthRequired` / `retired:` → `deletable:`

### 4.2 数据结构与探针

- [x] 包 `backend/internal/application/account/poolindex/`
  - `DispatchIndex`（有序切片 + map，O(log n) 级重排语义）
  - `DueHeap`（验证/普通/删除/调度探针）
  - `DRRScheduler`（5:3:2 / 验证空 7:3）
  - 单测：序、DRR 比例、堆行为
- [x] `RebuildBuildPoolIndex` / `syncAccountIndex`
- [x] `DispatchProbeTick`（最久未探堆）
- [x] `MaintenanceProbeTick`（DRR 三车道）
- [x] `GROK2API_BUILD_SAFE_PURGE_APPLY` 默认 true（compose/env）

### 4.3 路由与网关

- [x] Selector 接入 `OrderedDispatchIDs` / `NoteDispatchSelected`
- [x] Build Acquire 按调度索引重排后再试租约
- [x] Build 无 `observed_model` 硬跳过

### 4.4 API / 前端 / 外挂

- [x] `GET/PATCH .../build-probe` 四池 + 新 statistics/mode/outcome
- [x] Accounts 面板四池文案；去掉 recovery/retired/quarantine 展示语义
- [x] 外挂 `grok2api-account-pool-probe` **跳过 Build**

### 4.5 文档与部署

- [x] `docs/02-current-state.md`、`docs/logs/2026/2026-07.md` 已记四池上线
- [x] 标准链部署：GitHub Actions → GHCR → panda `compose pull`（禁止主机编译）

### 4.6 关键路径（便于接手）

| 能力 | 路径 |
| --- | --- |
| 四池 + 双探针核心 | `backend/internal/application/account/four_pool_probe.go` |
| 热路径索引 | `backend/internal/application/account/poolindex/` |
| 探针监控 DTO | `backend/internal/application/account/build_probe_monitor.go` |
| 启动双循环 / 迁移 | `backend/internal/app/startup.go` |
| 选号 | `backend/internal/application/gateway/selector.go` |
| HTTP | `backend/internal/transport/http/account/handler.go` |
| 面板 | `frontend/src/features/accounts/build-probe-panel.tsx` |
| 外挂 | `tools/grok2api-account-pool-probe.py` |

---

## 5. 部分完成 / 有意降级（需后续补齐）

| ID | 项 | 计划要求 | 现状 | 风险 | 建议优先级 |
| --- | --- | --- | --- | --- | --- |
| FP-P01 | 选号热路径 | `Acquire` 只走 `DispatchIndex.Ascend`，避免每次全表 | 仍 `ListRoutingCandidates` 全量 + TTL 缓存，再用索引**重排** | 账号量大时路由仍 O(n) 加载 | **P1** |
| FP-P02 | DispatchIndex 额度序 | key 含 `quota_remaining DESC`（未知排后） | Upsert 时常写 `QuotaKnown=false, QuotaRemaining=0`，额度维未真正生效 | 公平/额度优先偏差 | **P1** |
| FP-P03 | SQLite 覆盖索引 | `(provider, enabled, auth_status, observed_model)` + recovery `(account_id, status, next_probe_at)` | 现有 `idx_accounts_routing` **不含** `observed_model`；未见计划中的专用覆盖索引 | 冷启动/对账扫表偏慢 | **P2** |
| FP-P04 | Redis ZSET 镜像 | 启用 Redis 时调度索引可镜像 ZSET | 未实现；仅内存索引 | 多实例不一致（当前 Panda 单实例可接受） | **P2** |
| FP-P05 | BTree 实现 | `google/btree` 或等价红黑树 | 自研有序切片+map（语义接近，非标准 BTree） | 大 N 时常数因子 | **P3** |
| FP-P06 | DRR 生产占比观测 | 大样本近似 5:3:2 / 7:3 | 无 per-lane 计数暴露到 API/面板 | 无法证明权重未饿死 | **P1** |
| FP-P07 | 索引↔DB 对账 | 启动重建 + 运行期一致 | 仅启动重建；无定时对账/告警 | 偶发漏索引难发现 | **P2** |
| FP-P08 | 死代码清理 | 废除恢复/软退役 API | `ListRecoveryCandidates` / `ListPurgeCandidates` 仍在 repository 接口与实现 | 误用旧路径风险 | **P1** |
| FP-P09 | `retired:` 兼容 | 正式路径无 soft-retire | 多处仍识别 `retired:` 前缀（迁移/过滤兼容） | 可接受过渡；长期应只认 `deletable:` | **P2** |
| FP-P10 | `reauthRequired` 字段 | 不作长期驻留态 | DB/DTO 枚举仍保留；Build 标删除时仍可能写入该 auth_status | Web 仍用；Build 语义易混淆 | **P2** |

---

## 6. 未做（计划明确延后或未开工）

| ID | 项 | 说明 | 优先级 |
| --- | --- | --- | --- |
| FP-N01 | 层级时间轮 | 计划「可选增强 / 二期」：短冷却到期回调回 `DispatchIndex` | **P3**（二期） |
| FP-N02 | Selector 完全去全表 | 按 ID 批量 hydrate 前 k 候选，替代 `ListRoutingCandidates` 全量 | **P1** |
| FP-N03 | 调度失败阈值可配置 | 计划「建议 2 次，可配置」；当前常量 `buildDispatchFailLimit=2` | **P2** |
| FP-N04 | 面板区分双探针运行态 | 现监控偏「合并视角」；未分开展示调度探针 vs 维护探针各自 next/current | **P2** |
| FP-N05 | Analytics 快照四池化 | 历史 `account_pool_snapshots` / 趋势图或仍含旧语义字段（reauth 等） | **P2** |
| FP-N06 | 旧 backlog 文档改写 | `04-improvement-backlog` 中 BE-005/FE-003 仍写七池/恢复池（**过时**） | **P1**（文档债） |
| FP-N07 | 删除池清空后的持续观测 runbook | 已清零；需固定巡检项（delete>0 告警、dispatch 骤降） | **P1** |

---

## 7. 正式待办事项（Backlog）

状态词：`Todo` / `In Progress` / `Done` / `Dropped` / `Deferred`。

### P0 — 无（主路径已上线）

当前无阻塞生产的 P0 缺口。删除池已空、调度池稳定。

### P1 — 应尽快做

| ID | 标题 | 验收标准 | 状态 |
| --- | --- | --- | --- |
| **FP-001** | Selector 热路径去全表：按 `DispatchIndex` 取前 k ID 再批量加载候选 | Build `Acquire` 不再每次全表 `ListEnabled`；压测或单测证明候选加载 ≤k | Todo |
| **FP-002** | DispatchIndex 写入真实额度（billing/recovery） | Upsert 含 `QuotaKnown/QuotaRemaining`；同 priority 下高剩余优先 | Todo |
| **FP-003** | 探针 lane 计数进 `build-probe` statistics | API 含 verification/normal/delete/dispatch 尝试次数；验证空时 delete:normal≈3:7 可观测 | **Done**（`statistics.laneAttempts`） |
| **FP-004** | 删除无用恢复/purge 仓储 API | 移除或标注废弃 `ListRecoveryCandidates`/`ListPurgeCandidates`；全仓无调用 | **Done**（代码已无，2026-07-22 复核） |
| **FP-005** | 同步过时 backlog 文案 | 更新 BE-005/FE-003/02-current-state 残留「七池/恢复池」描述 | Todo |
| **FP-006** | 生产巡检 runbook | 文档写明：日检四池、`delete` 突增、`dispatch` 腰斩、外挂 probe `skipped_build` | Done（见 §11） |

### P2 — 增强与清理

| ID | 标题 | 验收标准 | 状态 |
| --- | --- | --- | --- |
| **FP-007** | SQLite 覆盖索引按计划补齐 | schema 含 observed_model 与 recovery(status,next_probe_at) 相关索引；EXPLAIN 冷查询走索引 | Todo |
| **FP-008** | 运行期索引↔DB 对账 | 定时或探针空闲时抽样对账；不一致打日志/指标 | Todo |
| **FP-009** | 双探针分轨面板 | UI 分别显示调度探针 / 维护探针的 current、next、最近结果 | Todo |
| **FP-010** | `buildDispatchFailLimit` 可配置 | env/settings 可调；默认 2 | Todo |
| **FP-011** | 收敛 `retired:` 兼容分支 | 仅迁移只读兼容或一次性 SQL 清洗后删除分支 | Todo |
| **FP-012** | Analytics/趋势四池化 | 快照与前端趋势去掉 recovery/retired 主导语义 | Todo |

### P3 / Deferred — 二期

| ID | 标题 | 验收标准 | 状态 |
| --- | --- | --- | --- |
| **FP-013** | 层级时间轮冷却回调 | 短冷却到期自动回调度索引，无需等普通池探针 | Deferred |
| **FP-014** | Redis ZSET 镜像 DispatchIndex | Redis 模式多实例共享调度序 | Deferred |
| **FP-015** | 引入 `google/btree`（若切片实现成瓶颈） | 基准显示收益后再换 | Deferred |

---

## 8. 建议执行顺序

1. **FP-005 + FP-006**（文档/巡检，成本低）  
2. **FP-004**（删死代码，防回退）  
3. **FP-003**（可观测 DRR，验证权重）  
4. **FP-002 → FP-001**（调度质量与复杂度）  
5. **FP-007 / FP-008**（持久层与对账）  
6. 其余 P2 / P3 按资源插入  

---

## 9. 与其它文档的关系

| 文档 | 关系 |
| --- | --- |
| 计划文件 `build池状态机优化_44771e70.plan.md` | 需求源；**不编辑** |
| [02-current-state.md](./02-current-state.md) | 总状态；四池结论以本文件细节为准 |
| [04-improvement-backlog.md](./04-improvement-backlog.md) | 长期池；应引用本文件 FP-* ID |
| [06-open-todos-2026-07-16.md](./06-open-todos-2026-07-16.md) | 旧开放待办（探活可视化等）；**已被四池方案部分取代** |
| [logs/2026/2026-07.md](./logs/2026/2026-07.md) | 月度流水 |

---

## 10. 变更记录

| 日期 | 说明 |
| --- | --- |
| 2026-07-21 | 四池双探针首版合并并部署（`e373afc`） |
| 2026-07-22 | 本盘点：删除池清零、调度稳定；列出 FP-001～FP-015 后续待办 |

---

## 11. 生产巡检 Runbook（FP-006）

频率：每日一次或变更后；命令均在 `ssh panda` 执行，**禁止**在主机编译。

### 11.1 健康与镜像

```bash
docker inspect grok2api --format '{{.Config.Image}} {{.State.Health.Status}}'
curl -fsS http://127.0.0.1:18000/healthz
curl -fsS https://grokimage.relai.asia/healthz
```

期望：`healthy` + `{"ok":true}`。

### 11.2 四池快照

```bash
PASS=$(cat /root/.secrets/grok2api-admin-password)
TOKEN=$(curl -fsS -X POST http://127.0.0.1:18000/api/admin/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$PASS\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["tokens"]["accessToken"])')
curl -fsS http://127.0.0.1:18000/api/admin/v1/accounts/build-probe \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

关注字段：

| 字段 | 正常 | 告警 |
| --- | --- | --- |
| `pools.dispatch` | 相对稳定（百级） | 腰斩或接近 0 |
| `pools.delete` | 通常 0 或短暂小幅 | 持续上升且 `deleted` 不涨（purge 关/卡死） |
| `pools.verification` | 导入后短暂升高再降 | 长期堆积 |
| `purgeApply` | `true`（除非紧急刹车） | 误关导致只打标不删 |
| `statistics.deleted` | 有 backlog 时应上涨 | 进程重启会清零属预期 |

### 11.3 外挂 probe

```bash
head -n 6 /opt/grok2api/tools/grok2api-account-pool-probe.py
journalctl -u grok2api-account-pool-probe.service -n 20 --no-pager
```

期望：脚本注明 Web/Console only；日志含 `skipped_build`，**不对** Build 调 refresh-token。

### 11.4 紧急刹车

```bash
# 停物理删除（保留 deletable 标记）
# 面板 Switch 关 purgeApply，或临时：
# GROK2API_BUILD_SAFE_PURGE_APPLY=false 后 compose up（仍禁止主机编译镜像）
```
