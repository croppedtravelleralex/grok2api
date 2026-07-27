# Lite 生图 vs Imagine 额度模型（2026-07-27）

> **状态**：事实档 + 实现口径。  
> **关联**：[http-reverse-lite-chain.md](./http-reverse-lite-chain.md)、[09-imagine-quota-model-state-and-10-concurrency-todos-2026-07-22.md](./09-imagine-quota-model-state-and-10-concurrency-todos-2026-07-22.md)、[22-proxies-concurrency-and-bottlenecks-2026-07-27.md](./22-proxies-concurrency-and-bottlenecks-2026-07-27.md)

## 1. Lite 和 Imagine 不是两个额度口，是两条生图路径

| | **Lite（Drawing REST）** | **Imagine（Pro / WS）** |
|---|---|---|
| 上游入口 | `POST /rest/app-chat/conversations/new` | `wss://grok.com/ws/imagine/listen` |
| 触发 | `Drawing: …` + `enableImageGeneration` | Imagine 页面 / WS 协议 |
| 能力 | 快速出图，分辨率较固定 | `aspect_ratio`、`1k/2k`、质量档 |
| grok2api 模型 | **`grok-imagine-image`**（主路径） | `grok-imagine-image-quality` 等 |

**对外「文生图」走 Lite；WS Imagine 是另一条 Pro 路径。**

## 2. 额度读数只有一个闸门字段：`imagine`

上游 **没有** `lite.allowance` 字段。Grok SPA 用：

`GET https://grok.com/rest/usage/free-usage-gates` → `imagine.allowance / imagine.remaining`

该字段命名是 **Imagine 产品线的免费闸门**，Lite 与 WS 很可能共用，或对 Lite **不适用**。

### 2.1 闸门返回形态

| 形态 | 示例 | 可读剩余次数 |
|------|------|-------------|
| 小整数 | `total=12, remaining=7` | **7 次**（直接） |
| micro-credit | `3850000000 / 3850000000` | **约 10 次**（`ImagineGenerations()` 换算） |
| **0/0** | `allowance="0", remaining="0"` | **未知**（不等于耗尽） |

### 2.2 Lite 本身不返回剩余次数

Lite 成功：HTTP 200 + 图 URL + 可能 `isSoftStop`。  
Lite 失败信号（探针，非计数）：

| 信号 | 含义 |
|------|------|
| `systemErrCode: 1010` | 明确 Imagine 额度为 0 |
| SSE / 流 `usage limit` | `usage_limit_reached` |
| HTTP 429 `code:8` | 请求过多（限速，≠闸门 0/0） |

**不存在「Lite 专用额度 API」。**

## 3. 2026-07-27 生产事实（北京时间）

| 现象 | 数值 |
|------|------|
| 压测成功段 | 约 **09:27–09:31** |
| 全池 imagine 同步为 0/0 | 约 **11:02–11:22** |
| enabled imagine DB | **671/671** `remaining=0` |
| Lite 复测仍可出图 | 1382/373/352/400 等 **200 OK** |
| 四池 image | dispatch=0，recovery=671（非 dead） |
| chat dispatch | **671**（账号未集体死亡） |

根因：**闸门 0/0 被当成耗尽**，调度要求 `total>0 && remaining>0`，与上游语义和 Lite 实测矛盾。

## 4. 我们怎么知道「能不能生」「还能生几次」

### 4.1 三层证据（实现口径）

```text
┌─────────────────────────────────────────────────────────┐
│ 层 1：闸门读数（优先）                                   │
│   free-usage-gates.imagine 有正数 / micro-credit        │
│   → 剩余次数 = ImagineGenerations(remaining, total)     │
├─────────────────────────────────────────────────────────┤
│ 层 2：模型状态（0/0 时）                                 │
│   available + 30min 内 Lite 成功 → 认为还能生（次数未知） │
│   quota_available / unknown → 待 L2 Lite 探针           │
├─────────────────────────────────────────────────────────┤
│ 层 3：Lite L2 探针（调度/维护探针）                      │
│   probeWebLiteL2：真实打一枪                             │
│   成功 → probe_lite_ok / available                      │
│   1010 / usage_limit → quota_exhausted                  │
└─────────────────────────────────────────────────────────┘
```

### 4.2 「还能生几次」的可答范围

| 闸门 | 能否回答「几次」 | 做法 |
|------|-----------------|------|
| 正整数 / micro-credit | **能** | `ImagineGenerations()` |
| 0/0 | **不能精确** | UI/调度标「未知」；pin 深度保守按 ≥1 |
| 明确 `total>0 && remaining=0` | **0 次** | 阻断 |
| Lite 探针失败 1010 / usage_limit | **0 次** | 写 `quota_exhausted` |

可选增强：从**已知正额度**起，每次 Lite 成功本地 `remaining--`（估算，上游不确认）。

### 4.3 代码落点（2026-07-27）

| 模块 | 职责 |
|------|------|
| `domain/account/imagine_quota.go` | `ImagineDispatchQuotaAdmissible`、0/0 语义 |
| `application/account/web_pool.go` | 图池 / dispatch 准入 |
| `application/gateway/selector.go` | 路由候选 |
| `application/gateway/image_stage_provider.go` | 生图前 refresh 仅阻断明确耗尽 |
| `application/account/web_pool_probe.go` | L2 Lite 探针（已有） |
| `tools/live_imagine_quota_audit.py` | 运维：闸门 + Lite 对照 |

## 5. 运维脚本

```bash
# Panda 上对照闸门读数与 Lite 探针（抽样）
ssh panda 'python3 -' < tools/live_imagine_quota_audit.py
```

输出列：`account_id | gate_remaining | gate_known | lite_http | lite_ok | model_status`

## 6. 与对话额度的关系

`POST /rest/rate-limits` 的 `fast/auto` **不是** Lite 生图额度。  
禁止用聊天 `remainingQueries` 推断生图次数。
