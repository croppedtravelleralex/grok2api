# Chrome 票生命周期实验（分批短跑，2026-07-23）

> **原则**：停止「一次跑满 30～60 分钟」的旧验收方式。每个 **Batch** 独立、可重复、**目标墙钟 ≤10 分钟**；出结论即停，不凑样本量。
>
> 架构与运维见 [13-chrome-ticket-pool-panda-api](./13-chrome-ticket-pool-panda-api-2026-07-23.md)；asset 下载已修见 commit `c07cc2e`。

---

## 0. 已证实事实（不必重复长测）

| 问题 | 当前结论 | 证据 |
|------|----------|------|
| 开票 IP ≠ 消费 IP？ | **可以不一致** | 本机 Chrome 开票 + Panda udeal 生图 1467 **200** |
| 池内一张票能否复用？ | **不能**（`Pop` 即 `consumed`） | Go `PopForAccount` 事务语义 |
| N 并发生图要 N 个 Chrome？ | **请求时 0 个**；灌池时 **1 个顺序浏览器** 即可 | 2026-07-23 验收 |
| `statsig_meta` 本体寿命？ | 约 **6～24h**（上游），与池 TTL 解耦 | Python/Go 注释 + 待 Batch S 量化 |
| asset 403（无票路径） | 已修：SSE 前 warm + Python 风格下载头 | `c07cc2e` + `pool_hit` 后 **200** |

以下实验用于 **量化边界** 和 **指导池深 / 浏览器数**，不是重复证明上表。

---

## 1. 本机 Chrome 持续出票

### 1.1 角色

| 组件 | 位置 | 频率 |
|------|------|------|
| `chrome_ticket_pool_minter.py` | **本机 Windows** | 后台循环，与 Panda QPS 解耦 |
| Go 票池 | Panda `chrome_tickets` | 消费端 pop |
| `grok-signer` | Panda sidecar | 每次生图现签 `x-statsig-id` |

### 1.2 单次灌票（~90s/张）

```powershell
# SSO 导出（按需刷新，不必每张票都做）
scp tools\_panda_export_sso.py panda:/tmp/
ssh panda "python3 /tmp/_panda_export_sso.py 1467" > .tmp\web-sso-canary-1467.json

# 入池（无 GROK2API_* 时走 SSH → Panda Admin API）
python tools\chrome_ticket_pool_minter.py `
  --account-ids 1467 `
  --sso-file .tmp\web-sso-canary-1467.json `
  --ttl-hours 12
```

### 1.3 持续出票（推荐）

**目标池深**：`available ≥ max(3, 并发档位 × 1.5)`。当前单账号 canary 建议维持 **3～5 张**。

```powershell
# 守护进程：每 5 分钟检查，不足则 mint
python tools\chrome_ticket_mint_daemon.py --account-ids 1467 --sso-file .tmp\web-sso-canary-1467.json --target 3

# 只补一次
python tools\chrome_ticket_mint_daemon.py --once --target 3
```

**注意**：

- 仅对 **有 imagine 额度** 的账号灌票（`tools/_panda_find_imagine.py`）。
- 本机代理 `127.0.0.1:7897` 须稳定；Chrome 默认有头（`GROK_PW_HEADLESS=0`）。
- 生产部署 digest：`sha256:49f23f31…7a841`（`c07cc2e`）。

### 1.4 池深告警（待接）

| 条件 | 动作 |
|------|------|
| `available < 2` 持续 10min | 告警 + 触发本机 minter |
| `consumed` 陡增但无 `pool_hit` | 查路由是否命中账号 |
| 连续 `asset 403` 且有 `pool_hit` | 查 [12](./12-web-lite-two-stage-failure-asset-403-2026-07-23.md) |

---

## 2. 实验方法论（替代旧长测）

### 2.1 停止的做法

| 旧做法 | 问题 |
|--------|------|
| 一次验收循环 8 次生图 + 等池空 | 15～40min，混因（池空/429/额度） |
| 无票时仍测「票是否有效」 | 走首页 meta 回退，**不是票实验** |
| 单脚本测 TTL + 并发 + 复用 | 失败难归因 |

### 2.2 新做法：Batch 制

```text
每 Batch：
  1. 写清假设 + 停止条件（≤10min）
  2. 准备条件（仅本步骤需要的票张数）
  3. 执行 → 记录 JSONL 一行
  4. 判定 pass/fail/inconclusive → 结束，不自动链下一 Batch
```

**记录模板**（`.tmp/chrome-ticket-experiments.jsonl`）：

```json
{"batch":"S1","ts":"2026-07-23T12:28:00Z","hypothesis":"meta valid at 30m","ticket_age_s":1800,"http":200,"pool_hit":true,"notes":""}
```

---

## 3. 实验矩阵（分 Batch）

### 组别一览

| Batch | 主题 | 墙钟 | 票消耗 | 回答的问题 |
|-------|------|------|--------|------------|
| **S0** | 基线 | ~3min | 1 | 当前链是否仍 200 |
| **S** | 存活时间 | ~10min×多轮 | 每检查点 1 | meta 多久仍可用 |
| **D** | 延迟用票 | ~5～10min | 3 | 开票后等 N 分钟再消费 |
| **R** | 复用 | ~5min | 2+ | 同 meta 能否二次消费 |
| **I** | 换 IP | ~5min | 2 | 消费侧换 egress 是否影响 |
| **C** | 跨 session | ~5min | 2 | 新浏览器会话重捕 vs 旧 meta |
| **M** | 多账号并发 | ~10min | ≤并发数 | 池深 vs 浏览器数 |

---

### Batch S0 — 基线（每次换 digest 后先跑）

```bash
# Panda
bash /opt/grok2api/tools/panda_chrometicket_image_acceptance.sh
# 期望：pool_hit + http=200，单次 try 即 break
```

**停止条件**：`http=200` 或明确失败类（403/502/1010）。

---

### Batch S — 票存活时间（测 TTL 上界）

**假设**：`statsig_meta` 在入池后仍有效，直至上游失效或 `expires_at` 清扫；与「开票 IP」无关。

**步骤**（每轮只测 **一个** 时间点，避免长循环）：

1. 本机 mint **1 张**票，记录 `created_at`（Admin API 响应）。
2. `sleep` 到目标龄：`0 / 5m / 15m / 30m / 1h / 3h / 6h / 12h`（**每次实验只选一个龄**）。
3. Panda 单次 `POST /v1/images/generations`（绑定 1467 的 Key）。
4. 记录：`pool_hit`、`http`、日志有无 `soft_stop` / `signature_failed`。

| 龄 | 期望（待填） | 实际（2026-07-23 1467） |
|----|--------------|-------------------------|
| 0 | 200 | ✅ S0 **200** pool_hit |
| 1m | ? | ✅ D-1m **200** pool_hit |
| 3m | ? | ✅ D-3m **200** pool_hit |
| 5m | ? | ✅ D-5m **200** pool_hit |
| 12h | ? | ⏳ 未测（需单独 Batch，勿与上表连跑） |

**能否无视 TTL 上界？**

- **池层 TTL**（默认 12h）：`sweep` 到期删行，与 meta 是否仍有效无关 → 可 `ttl_hours=24` 做对照 Batch，但 **不等于** 无视上游寿命。
- **上游 meta 寿命**：由 Batch S 实测曲线决定；**不能假设** 超过 24h 仍有效。
- **IP/指纹绑定**：票内 **无 IP**；设备 cookie 来自开票浏览器 → 换 IP 消费已证明可行；**换设备 cookie 未测**。

---

### Batch D — 延迟用票（三组）

| 组 | 开票后等待 | 票数 |
|----|------------|------|
| D0 | 0（对照） | 1 |
| D1 | 5 min | 1 |
| D2 | 30 min | 1 |

```text
mint → sleep → 单次 image API → 记录
```

**停止条件**：该组出结果即停；**不要**三组连续跑（会超 10min）。

---

### Batch R — 复用（跨请求）

**子实验**（分开跑）：

| ID | 操作 | 验证 |
|----|------|------|
| R1 | 正常 pop 消费一次 | 票变 `consumed` |
| R2 | 将 **同一** `statsig_meta` 再次 `POST /chrome-tickets` 入池，再消费 | meta 是否仍可签名+生图 |
| R3 | 不重新入池，用 `panda_lite_with_ticket.py` 直读 meta 文件打 Lite | 绕过池层的「二次消费」 |

**预期**：R1 单次；R2 **可能** 成功（测的是 meta 寿命，不是池记录复用）；R3 对照 Python 路径。

---

### Batch I — 换 IP（消费侧）

票 **不含** 出口 IP；消费走 Panda egress。

| 步骤 | 操作 |
|------|------|
| I1 | mint 1 张 → 立即生图（udeal web 出口 A） |
| I2 | mint 1 张 → Admin 暂改 `grok_web` 节点（出口 B）→ 生图 |

对比 `pool_hit` 后 `web_lite_asset_cf_warm` 与 `http`。若 I1/I2 均 200 → **消费 IP 可与开票 IP 不同，且换 egress 不绑死**。

---

### Batch C — 跨 session（开票侧）

| 步骤 | 操作 |
|------|------|
| C1 | Chrome session #1 mint → 立即消费 |
| C2 | **关闭浏览器**，新 session #2 再 mint（同账号 SSO）→ 消费 |
| C3 | session #1 的 meta **不入池**，session #2 只带新 meta | 旧 meta 是否已废 |

回答：开票是否必须同一 Playwright session；与「跨请求复用」正交。

---

### Batch M — 多账号高并发 vs 浏览器数

**核心问题**：10 并发生图要不要 10 个 Chrome？

| 事实 | 说明 |
|------|------|
| 生图请求时 | **0 Chrome**；pop + signer |
| 瓶颈 | **池深**、**SSESlots**、**账号 imagine 额度**、**udeal 出口** |
| 灌票速率 | 单浏览器 ~90s/张 → 10 张池深约 **15min** 灌满 |

**分批测**（每档独立 Batch，不要一次跑满 10）：

| 档 | 并发 | 前置池深 | Chrome |
|----|------|----------|--------|
| M1 | 2 | ≥2（可同账号两张票，或两账号各 1） | 1 浏览器顺序灌票 |
| M2 | 4 | ≥4 | 仍 **1** 浏览器；提前灌够 |
| M3 | 10 | ≥10 | 仍 **1** 浏览器；或 **2～3** 并行 context 仅加速灌池 |

**成功标准**：

- `pool_hit` 次数 = min(并发, 池深)
- 池深不足时：余请求走回退路径（**不算票实验失败**）

**何时需要多开浏览器？**

- **仅当**灌票速度 < 消费速度且不能接受池空窗口 → 2～3 个 Playwright context **并行 mint**，不是每请求一浏览器。

```bash
# M1 示例：Panda 上两路并发（两账号或两 Key 路由不同账号）
# 工具：tools/panda_web_serial_image_bench.py --concurrency 2（改并发参数若支持）
# 或两次并行 curl + 不同 request_id
```

---

## 4. 执行顺序（推荐）

```text
S0（部署后必跑）
  → 本机开启持续 minter（池深≥3）
  → D0 → D1 → D2（各开一次会话）
  → S@5m → S@30m → …（每龄一轮）
  → R2 / I2 / C2（按需）
  → M1 → M2（账号池够再 M3）
```

每完成一项，在 `.tmp/chrome-ticket-experiments.jsonl` 追加一行，并更新 [13](./13-chrome-ticket-pool-panda-api-2026-07-23.md) 验收表。

---

## 5. 与代码行为的映射

| 实验项 | 代码位置 |
|--------|----------|
| pop 即消费 | `chrometicket/pool.go` `PopForAccount` |
| TTL / sweep | `Push` TTL + 15min `sweep` |
| 无票回退 | `attachChromeTicket` 无票不改 ctx |
| CF warm | `prepareChromeTicketDownloadCookie`（SSE 前） |
| 下载 cookie | `resolveAssetDownloadCookie` |
| 同账号 Lite 并发 1 | imagepipeline lease |

---

## 6. 工具（已实现）

| 工具 | 作用 |
|------|------|
| `tools/chrome_ticket_experiment_lib.py` | 共享：mint、入池、生图、JSONL |
| `tools/chrome_ticket_experiment_log.py` | `append` / `list` 实验记录 |
| `tools/chrome_ticket_batch_run.py` | 统一入口：`s0` `delay` `survival` `reuse` `cross-session` `concurrent` `stats` |
| `tools/chrome_ticket_batch_delay.py` | Batch D 薄封装：`--wait 5m` |
| `tools/chrome_ticket_batch_s0.sh` | Batch S0 + stats + 写日志 |
| `tools/chrome_ticket_mint_daemon.py` | 本机持续灌池（`--target 3`） |

### 快速命令

```powershell
# 池统计
python tools\chrome_ticket_batch_run.py stats

# S0 基线（无票时自动 mint）
python tools\chrome_ticket_batch_run.py s0 --mint

# D1：开票后等 5 分钟再消费
python tools\chrome_ticket_batch_delay.py --wait 5m

# S@30m 存活
python tools\chrome_ticket_batch_run.py survival --age 30m

# R2：同 meta 再入池消费
python tools\chrome_ticket_batch_run.py reuse --variant r2

# M2：2 路并发（先灌 2 张票）
python tools\chrome_ticket_batch_run.py concurrent -n 2 --mint

# 持续灌池
python tools\chrome_ticket_mint_daemon.py --account-ids 1467 --target 3

# 查看最近记录
python tools\chrome_ticket_experiment_log.py list --last 5
```

日志默认：`.tmp/chrome-ticket-experiments.jsonl`

### 2026-07-23 实测摘要（账号 1467，分批执行）

| Batch | 结论 | 关键数据 |
|-------|------|----------|
| S0 | ✅ pass | http=200, pool_hit=true（本机开票 → Panda 消费，**跨 IP**） |
| D-1m / D-3m / D-5m | ✅ pass | 延迟 60/180/300s 后均 200 + pool_hit |
| R2 跨请求复用 | ⚠️ meta 可复用 | 同 `statsig_meta` 再入池后 **两次 pool_hit**；当次 http=429（额度，非票失效） |
| C1 / C2 跨 session | ⚠️ 开票不绑 session | 独立 subprocess 开票 + pool_hit；http=429 |
| TTL24 | ⚠️ 池 TTL 可配 24h | `expires_at` +24h；immediate consume pool_hit；**不能**推断可无视上游 meta 寿命 |
| M2 并发 | ✅ 不需多浏览器 | 2 路并发 **pool_hits=2**（单账号）；http=429 为额度；灌票仍 **1 Chrome 顺序** |

**池记录**：`Pop` 即 `consumed`，**不能**同一条池记录复用。  
**meta 内容**：可再次 `POST` 入池后消费（R2 证实）。  
**多并发**：吃并发靠 **池深 + SSE 槽位**，不是请求时多开 Chrome。

---

## 相关文档

- [13-chrome-ticket-pool-panda-api-2026-07-23.md](./13-chrome-ticket-pool-panda-api-2026-07-23.md)
- [12-web-lite-two-stage-failure-asset-403-2026-07-23.md](./12-web-lite-two-stage-failure-asset-403-2026-07-23.md)
