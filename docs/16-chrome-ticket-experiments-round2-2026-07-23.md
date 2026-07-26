# Chrome 票实验第二轮（2026-07-23 晚）

> 会话：[Chrome 票池与 Admin 密码](397f17e4-fbe6-4f5b-a416-a5b40b8f642a)  
> 密码根因子任务：[密码不同步调查](8136e7b9-276d-40ec-8714-654fcd2df9d5)  
> 日志：`.tmp/chrome-ticket-experiments.jsonl`  
> 原则：出错即停；分批 ≤10min；有票路径与额度/限速分开统计。

## Admin 密码事件（已处理）

| 项 | 说明 |
|----|------|
| 现象 | `/root/.secrets/grok2api-admin-password`（7/17）与 DB `admins.password_hash` 不一致 → 401 |
| 时间线 | 22:08 login **200** + chrome-tickets **201**；约 22:42 起同 secrets **401**；23:09 import 仍 login **200** |
| 根因（高置信） | **三套密码源不同步**：① secrets（7/17）；② `/opt/grok2api/tools/grok2api-reset-admin-password.sh` 硬编码另一口令，只写 DB+secrets；③ `/root/import-fresh-accounts.py` 读 `/root/.grok2api-admin-password.tmp`（与 secrets 无关） |
| 非因 | 用户未改；本会话未写 secrets；无 cron 自动轮换 |
| 处置 | 2026-07-23 23:22 重置为约定口令，同步 secrets + staging + `.tmp`，login **200** |
| 工具 | `tools/_panda_reset_admin_password.py`、`_panda_admin_token.py`（多路径 fallback） |

## 第一轮短批（下午，已证实）

账号 **1467**；本机 Chrome 开票 + Panda udeal 消费。

| Batch | 结果 | http | pool_hit | wall_ms | 结论 |
|-------|------|------|----------|---------|------|
| S0 立即消费 | pass | 200 | true | 9806 | 跨网可用 |
| D-1m | pass | 200 | true | 9094 | 延迟 1min OK |
| D-3m（×2） | pass | 200 | true | 14681 / 8854 | 延迟 3min OK |
| D-5m | pass | 200 | true | 8483 | 延迟 5min OK |
| R2 同 meta 再入池 | fail | 429/429 | true/true | — | **票可再 pop**；失败为额度/限速 |
| C1/C2 新 Playwright 子进程 | fail | 429 | true | — | 跨 session 票路径 OK；撞限速 |
| TTL24 立即 | fail | 429 | true | 2616 | 同上 |
| M2 并发 2 | inconclusive | 0×200 | ≥2 hits | — | 全 429，无法验带宽 |

据此写入 [15](./15-chrome-ticket-cf403-ip-reframe-2026-07-23.md)：开票 IP ≠ 消费 IP **可行**；Pop 后同 meta 可再入池；429 ≠ 票失效。

## 实验矩阵与工具（第二轮）

| 批次 | 命令 | 通过标准 |
|------|------|----------|
| R-delay-0/1m/5m/30m | `chrome_ticket_reuse_delay_runner.py --delay <gap>` | 两次 probe：http=200 + pool_hit |
| S-10m…12h | `chrome_ticket_survival_runner.py` / orchestrator | 等待后 probe 200 + pool_hit |
| V-serial ×5 | `validation_runner.py --phase serial` | 每轮 200 + pool_hit |
| V-conc 3×10 | `validation_runner.py --phase concurrent` | 10×200，记录带宽 |
| mint_fast | `chrome_ticket_mint_fast.py --workers 1/2` | avg_s 对比 |

前置：`python3 /tmp/_panda_prepare_imagine.py <account_id>`（pin + refresh quota）

## 带宽记录字段（probe）

- `wall_ms`：生图 API 墙钟
- `download_mbps` / `download_bytes` / `download_ms`：asset URL 单图下载
- `pool_hit` / `pool_hits`：docker logs 命中

## 第二轮结果（2026-07-23 晚：停于限速）

| Batch | 结果 | http | pool_hit | 备注 |
|-------|------|------|----------|------|
| R-delay-0（多次） | **停** | 429 / 503 | 有时 true | 1467 Imagine 速率限制 |
| R-delay 其余档 | 未跑 | | | 当晚停 |

## 第三轮结果（2026-07-24：R-delay 全档通过）

账号 **1574**（`thomas43nu@yumail.co`）；本机 Chrome 开票 → Panda udeal 消费；`stop-on-fail`。

| Batch | 结果 | gap | 1st http/wall_ms | 2nd http/wall_ms | pool_hit |
|-------|------|-----|------------------|------------------|----------|
| R-delay-0 | **pass** | 0 | 200 / ~8.7–13.7s | 200 / ~8.4–8.6s | true/true |
| R-delay-1m | **pass** | 60s | 200 / ~7.6–8.5s | 200 / ~8.7–9.0s | true/true |
| R-delay-5m | **pass** | 300s | 200 / ~8.8–10.3s | 200 / ~8.9–9.1s | true/true |
| R-delay-30m | **pass** | 1800s | 200 / ~8.8–9.0s | 200 / ~12.1–13.3s | true/true |

**结论**：同 `statsig_meta` 再入池后，间隔 0 / 1m / 5m / 30m 均可再次 `pool_hit` + 生图 **200**。池记录仍单次消费；复用靠 meta re-push。

### 第三轮排障笔记（必读）

| 问题 | 处置 |
|------|------|
| 无 `survival-state/`（昨晚） | S-10m mint 时 admin 401，无可验收挂起票 |
| 1467 code8 限速 | 换 1574 / 1507 |
| 独占 pin ∩ 过期内存图池 → 503 | pin 须在 image dispatch 内，或重启 `grok2api` |
| **udeal 出口冷却** | 见下 FAQ；与「票跨 IP」是两层问题 |
| acct 88 探针刷 429 | 实验时临时 disable，已恢复 |
| probe `download_http=403` | media URL 裸 GET 无鉴权；生图 API 200 仍成立 |

## 第四轮结果（2026-07-24 白天：S / V）

### S 存活（白话：开票后等 N 分钟再消费）

| Batch | 结果 | 账号 | 说明 |
|-------|------|------|------|
| S-10m | **pass** | 1507 | 等 10min 后 200+pool_hit |
| S-15m | 抖动 | 1507 | 末次 503+pool_hit |
| S-30m | **pass** | 1507 | 等 30min 后 200 |
| **S-60m** | **pass** | 1574 | 等 60min 后 200 → **当前证据：票至少能活 60 分钟**（未证 3h+） |
| S-3h | mint 已挂 | 1574 | 10:08 开票，13:08 起可验收 |
| S-6h / S-12h | mint 已挂 | 1574 | 16:10 / 22:12 起可验收 |

**S-60m 含义**：不是「票只能活 60 分钟」，而是**目前只测到 60 分钟仍有效**。

### V 串行

| Round | 结果 | 账号 |
|-------|------|------|
| 1–4 | **pass** 200 | 1574 / 92 / 1507 / 1467 |
| 5 | **停** 502+pool_hit | 1574 |
| V-conc | **未跑** | serial 出错即停 |
| mint_fast | **未跑** | |

### FAQ：udeal 冷却 vs 票跨 IP；Webshare 出图？

| 层 | 是什么 | 和票的关系 |
|----|--------|------------|
| **票** | statsig_meta + 设备 cookie | **不绑 IP**；已证实跨 IP/session |
| **出口** | Panda 访问 grok.com 的代理 | 与票无关；冷却 → 503「无可用出口」 |

**udeal 出口冷却**：udeal 节点因失败被暂时禁用，不是票过期。  
**Webshare 出图**：**不行**。Webshare 对 grok.com 是 CF challenge，生产 `grok_web` 全关。票可跨 IP，但出口必须能过 CF——目前只有 udeal LA 口可用。

### 待办

| Batch | 状态 |
|-------|------|
| S-3h consume | **冻结**（已到期，待双池门禁后验收） |
| S-6h / S-12h | 挂起等待 |
| V-serial-5 重试 + V-conc | **冻结** |
| mint_fast | **冻结** |

> **2026-07-24**：生图/票压测全部暂停，先完成 [plan.md](./plan.md) 双池 + 流量统计。

## 下一步

1. 实施 plan **BE-019**（号池）→ **BE-018**（流量）→ **BE-021**（票池联动）。
2. 通过 plan §4 门禁后恢复本表实验。
3. 多 subagent 按 plan §5 推进。

## 相关

- [14-chrome-ticket-lifecycle-experiments](./14-chrome-ticket-lifecycle-experiments-2026-07-23.md)
- [15-chrome-ticket-cf403-ip-reframe](./15-chrome-ticket-cf403-ip-reframe-2026-07-23.md)
- [13-chrome-ticket-pool-panda-api](./13-chrome-ticket-pool-panda-api-2026-07-23.md)
