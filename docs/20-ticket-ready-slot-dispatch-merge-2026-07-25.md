# 票池并入号池：TicketReady 销票槽位（2026-07-25）

> **状态**：架构共识档 + 运维事实归档。  
> **关联**：[plan.md](./plan.md) §2–3、[13-chrome-ticket-pool-panda-api](./13-chrome-ticket-pool-panda-api-2026-07-23.md)、[17-web-four-pool](./17-web-four-pool-and-imaging-success-rates-2026-07-24.md)、[07-udeal-zero-browser-ops](./07-udeal-zero-browser-ops-2026-07-21.md)。

## 结论摘要

| 项 | 结论 |
|----|------|
| 双池合并方向 | **赞成**：票作为「号上的挂载物」，调度只认 **TicketReady** |
| 物理存储 | `chrome_tickets` 表 **保留**；合并的是 **调度语义**，不是删表 |
| 当前主因 | 票池、dispatch、pin、runtime **四套集合**无单一真相 → 孤儿票、runtime 被压扁 |
| 推荐模型 | **销票槽位（ImagineSlot / SlotRegistry）**，非简单把两个计数器相加 |
| 下一步工单 | **BE-024**（见 [04-improvement-backlog](./04-improvement-backlog.md)） |

---

## 1. 现状：四套集合叠加

```text
dispatch（imageDispatchPoolIds）  ≈ 四池合格 + Imagine 额度新鲜（~30min）
pin（imagePinIds / model_route）  ≈ grok-imagine-image 路由白名单
runtime（imagePoolIds）           ≈ dispatchIndex ∩ pin（cap 50）
票池（chrome_tickets）            ≈ 按 account_id 绑定，Pop 即 consumed
```

生图硬约束（Go）：

- `grok-imagine-image`：**必须有票**（`requiresChromeTicketAdmission`）
- 选号：`filterChromeTicketCandidates` 过滤无票号
- 取票：`PopForAccount(account_id)` — 票已按号存储，但 **入 dispatch 不看票**

### 1.1 指标勿混读

| 指标 | 含义 | 常见误读 |
|------|------|----------|
| `imageSchedulableRemaining`（~360） | dispatch 额度**次数**之和 | ≠ 可并发销票槽位数 |
| `available_tickets`（如 19） | 池内 available 票张数 | ≠ 19 路可并发 |
| `imageDispatchPoolIds`（如 64） | 四池快照合格账号数 | ≠ 都能销票 |
| `imagePoolIds` / runtime | pin 约束后的索引投影 | 可远小于持票数 |

**可销票生图**应定义为：

```text
TicketReady = dispatch合格 ∩ pin允许 ∩ available_tickets>0
```

---

## 2. 2026-07-25 Panda 实测（归档）

### 2.1 票与调度漂移

| 维度 | 观测值 | 说明 |
|------|--------|------|
| available 票 | 19 | 与「灌 ~30、消费 ~11」一致 |
| 持票账号 | 19 | 每号 1 张 |
| imagine pin | 3 | 250、263、342 |
| runtime | 1～3 | pin sync 收窄后曾只剩 342 |
| 真孤儿（有票 ∉ dispatch） | 6 | 87、227、228、250、263、276 等（随索引刷新波动） |
| 有票 ∉ pin | 16 | 多数仍在 dispatch，但路由不可见 |

**不是**「半小时内 16 个号掉出 dispatch」为主因；主因是：

1. **票开在非 pin / 非稳定 dispatch 号上**（JIT 未绑定槽位）
2. **pin sync** 在有票时缩成 `dispatch ∩ 持票号` → runtime 被票分布绑架
3. **dispatch 出池与票生命周期脱钩**（见 §3）

### 2.2 探针与工具（本机）

| 工具 | 作用 |
|------|------|
| `tools/panda_unified_pool_snapshot.py` | 单次 SSH：web-pools + stats + lane-quota |
| `tools/chrome_ticket_pool_probe.py --remediate` | pin sync + 清孤儿 + readiness |
| `tools/chrome_ticket_probe_rs/` | Rust 探针（dispatch/normal/ticket-quota） |
| `tools/chrome_ticket_jit_mint_concurrent.py` | 并发灌票 |
| `tools/chrome_ticket_experiment_lib.py` | `preflight_mint_gate`、`purge_orphan_tickets` |

Readiness blocker 示例：`ticket_pool_available=19 < 30`、`runtime_ticket_accounts=3 < 6`。

---

## 3. dispatch 进出：频繁吗？未使用会掉吗？

**不是秒级随机乱动**；`dispatchIndex` 无「闲置 N 分钟自动踢」定时器。但 **未发图也会掉**，触发条件是 **资格重算**，不是「用过才踢」。

### 3.1 入 dispatch 硬门槛（`imageDispatchAdmissible`）

- `enabled` + `auth=active` + 非账号冷却
- Imagine 额度 **新鲜**：`SyncedAt` 在 **30 分钟内**（`imagineQuotaFreshTTL`）
- `modelState` ∈ `available` / `quota_available`
- 无 imagine block（或 block 时仍有正额度）

代码：`backend/internal/application/account/web_pool.go`。

### 3.2 出池触发

| 触发 | 典型后果 |
|------|----------|
| Imagine `SyncedAt` > 30min | 下次 `syncWebAccountIndex` / 探针轮到该号时出 dispatch |
| dispatch 探针失败 | **15min** 账号 `cooldown_until` + 立即 sync 出池 |
| reauth / signature_failed | 进 delete 池 |
| pin 变更 + `RebuildWebPoolIndex` | runtime 投影变化 |

探针默认：**30s** 调度 tick，image/chat **交替**（图轨约 ~60s 一轮）；`IdleInterval` 5min。见 `config/web_probe.go`。

### 3.3 与票的关系

- 票绑在 `account_id` 上，**账号出 dispatch 时票不自动迁移**
- 故产生 **孤儿票**（`orphan_ticket` = 有票且 `account_id ∉ imageDispatchPoolIds`）
- 250/263 曾出现 **在 pin、不在 dispatch**（`pin_not_in_dispatch`）

---

## 4. 双池合并方案：销票槽位（推荐）

### 4.1 原则

> **Imagine 生图只有一条调度视图：槽位号 + 挂载票。无票不进 TicketReady；出池则清票或阻塞出池。**

物理上 `chrome_tickets` 仍可独立表；逻辑上合并为 **ImagineSlot**：

```text
ImagineSlot {
  account_id
  dispatch_eligible      // 四池 + 额度新鲜 + 未冷却
  available_tickets      // 通常 0/1（一槽一票）
  ticket_expires_at
  pin_bound
}
TicketReady = { slot | dispatch_eligible ∧ available_tickets > 0 }
```

### 4.2 与「硬合并 dispatch」的区别

| 方案 | 做法 | 评价 |
|------|------|------|
| A. dispatch 入池强制 `tickets>0` | 无票不进 dispatch | 鸡生蛋：JIT 要先有槽位 |
| **B. SlotRegistry（推荐）** | 固定 N 个销票槽；只在此灌票；pin=dispatch∩槽 | 稳定、可运维 |
| C. 仅 API 合并快照 | 展示 `ticketReadyIds` | 治标，漂移仍在 |

### 4.3 运维契约

| 操作 | 规则 |
|------|------|
| JIT 开票 | **仅** `SlotRegistry` 内且 `dispatch_eligible` 的号 |
| pin sync | `target = SlotRegistry ∩ dispatch`（**不要**缩成「当前谁有票」） |
| 出 dispatch | 有 available 票 → **expire 票** 或 **阻塞出池**（可配置） |
| 销票后 | **保留槽位**，JIT 补 1 张；并发由 `len(TicketReady)` 决定 |
| 范围 | **仅** `grok-imagine-image` 轨，不影响 chat dispatch |

### 4.4 实施阶段

| 阶段 | 内容 |
|------|------|
| **P0** | `TicketReady` Admin/探针指标；`SlotRegistry` 配置；JIT `preflight` 只认槽位；孤儿清扫 |
| **P1** | 有票号出池前优先 L0 刷新额度；Push 票后 `syncWebAccountIndex`；选号只 hydrate `TicketReady` |
| **P2** | `account_imagine_slots` 表或 Admin 槽位 UI；pin=槽位全集 |

### 4.5 待拍板参数

1. **槽位数 N**：6（并发）vs 30（缓冲）？
2. **出池策略**：有票时 **阻塞出池** vs **自动 expire 票**？

---

## 5. 已有代码触点（合并时改哪里）

| 模块 | 文件 | 说明 |
|------|------|------|
| pin 收窄 | `web_pool_pins.go` `imageDispatchPinTargetIDs` | 有票时缩 pin → 需改为 SlotRegistry 驱动 |
| 选号 | `selector.go` `filterChromeTicketCandidates`、`ticketHolderIDs` | 已按票过滤；应收敛到 TicketReady 单路径 |
| 孤儿定义 | `chrome_ticket_experiment_lib.py` | `orphan_ticket = avail>0 ∧ aid∉dispatch` |
| 票池 | `chrometicket/pool.go` `PopForAccount` | 保持；槽位是调度层概念 |
| 探针 | `chrome_ticket_pool_probe.py` / `probe_rs` | 增加 `ticket_ready_accounts` |

---

## 6. 同周其他生产变更（归档）

### 6.1 出口冷却关闭

- 配置：`routing.disableCooldown`（默认 `true`）
- 效果：egress 5xx/传输失败 **不再写** `cooldown_until`；账号 `MarkFailure` / soft-stop 退避关闭
- **保留**：上游 429 / Imagine 额度 block（平台限速，非失败退避）
- 代码：`backend/internal/infra/egress/manager.go`、`gateway/selector.go`、`config/config.go`
- Panda：2026-07-25 曾以 `grok2api-disable-cooldown-v9` 二进制替换；**正式须走 GHCR**（见 [05-playbook](./05-ai-maintenance-playbook.md)）

### 6.2 udeal 出口（不变）

- 生产仅用 LA 粘滞 `70.39.164.200:30000`；**禁止** `as.udealproxy.com:6666` 旋转池
- `grok_web`（node 110）与 `grok_web_asset`（node 111）分工；asset health 低 → asset403，非票问题
- 详见 [07](./07-udeal-zero-browser-ops-2026-07-21.md)

---

## 7. 503 / 失败分层（销票路径）

| 文案 / 现象 | 层 | 处理 |
|-------------|-----|------|
| `chrome_ticket_unavailable` | 无票 / 选号过滤后无票 | 灌票到 TicketReady 槽位 |
| `没有可用上游账号` | 号池 / pin / dispatch 空 | pin sync、四池、额度新鲜 |
| `当前没有可用的 grok_web 出口节点` | egress（冷却已关则查 health/单节点） | [07](./07-udeal-zero-browser-ops-2026-07-21.md) |
| 502 soft_stop / upstream | Lite SSE 阶段 | 换号、model state |
| asset 403 | `grok_web_asset` 出口 | node 111 anti-bot |

---

## 8. 常用命令

```bash
# 探针 + remediate（pin sync、清孤儿）
python tools/chrome_ticket_pool_probe.py --remediate -c 6 -n 30 --python-only -w 16

# JIT 灌票（须先 preflight / SlotRegistry）
python tools/chrome_ticket_jit_mint_concurrent.py -n 11 -j 6 -w 16

# 串行 / 并发验收生图
python tools/chrome_ticket_verify_images.py -n 6 --gap 3
python tools/chrome_ticket_verify_images_concurrent.py -n 6 -j 6
```

---

## 9. 文档与工单索引

- 实施计划母档：[plan.md](./plan.md)
- 票池 API：[13](./13-chrome-ticket-pool-panda-api-2026-07-23.md)
- 四池与成功率：[17](./17-web-four-pool-and-imaging-success-rates-2026-07-24.md)
- 改进工单：**BE-024** TicketReady 槽位合并（[04](./04-improvement-backlog.md)）
