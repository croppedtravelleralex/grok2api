# 新代理与并发能力验证（2026-07-27）

> **状态**：架构变更结论档 + 待执行方案。  
> **关联**：[21-ticket-obsolescence-and-asset403-tls-2026-07-26.md](./21-ticket-obsolescence-and-asset403-tls-2026-07-26.md)、[20-ticket-ready-slot-dispatch-merge-2026-07-25.md](./20-ticket-ready-slot-dispatch-merge-2026-07-25.md)、
> [17-web-four-pool-and-imaging-success-rates-2026-07-24.md](./17-web-four-pool-and-imaging-success-rates-2026-07-24.md)

## 结论摘要

| 项 | 旧认知 | 实测结论 |
|----|--------|----------|
| `webConcurrency=2` 是生产安全值 | 是 | **否**。udeal 单线扛 30 并发认证请求 **30/30 200**，p50 只从 1.68s 涨到 2.18s，零 CF 挑战。2 并发是纯自我设限 |
| Webshare 可作 `grok_web` | **不能**，doc 07 已确认 | 仍 **不能**。120 条新代理（20 住宅 + 100 升级机房）打 grok.com 全部 403 CF challenge |
| 代理可作 `grok_web_asset` | 未经验证 | **可以**。120/120 穿透 assets.grok.com 无挑战，住宅线延迟中位 320–410ms |
| panda 下行带宽 | doc 02 记录 ~15 Mbps（单 udeal 口旧测量） | **实测 108–180 Mbps**。20 住宅并发拉 CF 测速文件 10 秒 150.5 Mbps，聚合吞吐远超旧值 |
| 原有 100 条机房线 | 低质量 | **已升级**：与住宅线一样可穿透 asset 口，延迟中位 280–350ms（略优于住宅） |

---

## 1. 代理验证

2026-07-27 拿到两批新代理：

- **20 住宅**（`Webshare 20 proxies.txt`）：`host:port:user:pass`，IP 白名单 `43.156.233.219`（panda 出口 IP），无带宽限制
- **100 机房**（`Webshare 100 proxies (1).txt`）：与原 webshare 同供应商但升级质量

从 panda 实测：

| 来源 | 可用率 | 穿透 assets.grok.com | 穿透 grok.com | 建议用途 |
|------|--------|----------------------|---------------|----------|
| 20 住宅 | 20/20 可用，rtt 中位 ~1s | **200 无挑战**，320–410ms | **403 CF challenge（120/120）** | `grok_web_asset` |
| 100 升级机房 | 抽样可通 | **200 无挑战**，280–350ms | 同 | 备用 asset 下行 |
| udeal 单线 | 健康 | 200（但 node 111 已劣化到 health=0.05） | **200** | SSE / 线上请求 |

### 1.1 带宽实测

20 住宅代理并发拉 CF 测速文件：

| 并发 | 墙钟 | 聚合带宽 |
|------|------|---------|
| 1 | 0.7s | 108.5 Mbps |
| 5 | 3.5s | 114.2 Mbps |
| 10 | 4.5s | **179.7 Mbps**（峰值） |
| 20 | 10.6s | 150.5 Mbps |

panda 实测下行 108–180 Mbps，远高于 doc 02 记录的 ~15 Mbps（单 udeal 口无代理的旧测量）。
单张图约 200KB，理论可支撑 **70–115 并发下载**而不被带宽卡住。

---

## 2. 并发能力验证（udeal 单线）

带真实 SSO 凭证的首页 GET，通过 udeal 出口：

| 并发 | 墙钟 | p50 | 最慢 | CF 挑战 | meta 提取率 |
|------|------|-----|------|---------|------------|
| 10 | 2.1s | 1.68s | 2.05s | 0/10 | 10/10 |
| 20 | 2.2s | 1.85s | 2.20s | 0/20 | 20/20 |
| 30 | 2.5s | 2.18s | 2.48s | 0/30 | 30/30 |

**结论**：30 并发零失败，延迟几乎不随并发增长。`webConcurrency=2` 是**纯自我设限**。

而上游 API 1.2 秒就能应答的生产场景，并发 8 时压测却 7.5 秒/张 —— 46 秒以上花在
`webGate` 门禁排队里。当前吞吐瓶颈完全在人工设的门禁上，不在网络、不在上游。

---

## 3. 已执行操作

### 3.1 注册 20 条住宅线为 asset 下行节点

脚本 `tools/register_asset_nodes.py`（幂等 POST `/api/admin/v1/egress-nodes`）。

创建节点名 `wsres-asset-001` ~ `wsres-asset-020`，scope `grok_web_asset`：

```
APPLIED: created=20 skipped=0 failed=0
```

### 3.2 100 条升级机房线（2026-07-27 复测）

上传至 panda `/root/.secrets/webshare100_v2.txt`。抽样 15 条用 `tools/test_webshare_proxies.py`：

| 目标 | 结果 | 结论 |
|------|------|------|
| `https://grok.com/` | 15/15 可达（403 CF） | 与住宅线一致，**不能**作 `grok_web` |
| `https://assets.grok.com/` 根路径 | 15/15 **404**（plain curl） | 根路径探针无效；此前 doc 用真实 asset URL 得 200。**暂不注册**为 egress 节点 |

### 3.3 asset cookie 根因（已修复，非 affinity 哈希环）

### 3.4 udeal-111 已恢复

```
id=111 name=udeal-la-grok_web_asset enabled=1
```

当前 `grok_web_asset` 启用节点 = 21（udeal-111 + 20 住宅），全部 `enabled=1`。

---

## 4. 现有架构瓶颈与改进方向

| 层 | 当前值 | 瓶颈？ | 目标 |
|----|--------|--------|------|
| `webConcurrency` | 2 | **是**。30 并发 udeal 实测零失败 | 分阶段 2→8→20 |
| `SSESlots` | 8（代码默认） | 随并发抬升会满 | 同步扩大到 20 |
| `imageSlots` | 10（代码默认） | 同上 | 同步扩大 |
| `queueSize` | 100（代码默认） | 20 并发下暂不紧缺 | 维持或抬到 200 |
| 下行带宽 | 实测 108–180 Mbps | **否**。理论支撑 70–115 并发下载 | 暂不限制 |
| asset 节点健康 | udeal-111 health=0.05 | **是**。劣化到地板 | 住宅线接管后应改善 |
| pin 同步 | 5min 定时器 | 无（dispatchImageLen=149） | 维持 |
| catchup 刷新 | 5.9min/50 个 | 无（79min 全池 > TTL 30min 是结构性） | 可加，不阻塞 |

---

## 5. 待办（按建议执行顺序）

| P | 事项 | 状态 |
|---|------|------|
| ~~P0~~ | asset cookie 住宅修复 + 禁 111 验证 | **Done** |
| ~~P0~~ | web20 profile + 10/20 并发验收 | **Done**（见 §8） |
| ~~P1~~ | `grok2api-web-clearance.service` | **Done**（FlareSolverr `127.0.0.1:18191` + wrapper） |
| ~~P1~~ | `image_pipeline_traces` 陈旧 running | **Done**（80 条 `stale_abandoned`；代码每小时清扫） |
| ~~P2~~ | BE-024 pin 与 SlotRegistry 对齐 | **Done**（`imagineSlotAccountIds` 非空时收窄 pin） |
| P2 | 100 机房注册为 asset 节点 | **暂缓**（根路径 curl 404；需真实 asset URL 探针后再定） |
| P2 | 10/20 **100%** 成功率 | **未达**（10→9/10、20→19/20；SSE `soft_stop` / 502） |

---

## 8. 10 / 20 并发验收耗时（2026-07-27，镜像 `sha256:1d8c90a…`）

配置：`webConcurrency=20`，`promptSlots/sseSlots=20`，`queueCapacity=200`，住宅 `wsres-asset-001~020` 已注册。

### 8.1 墙钟与成功率（canary 脚本）

| 并发 | 成功 | 墙钟 | 客户端 P50 | 客户端 P90 | 失败模式 |
|------|------|------|------------|------------|----------|
| **10** | **9/10** | **~58s** | ~26s | ~54s | 1×502 `upstream_unavailable` |
| **20** | **19/20** | **~67s** | ~34s | ~48s | 1×502 `soft_stop`（trace `LGGfGHAL`） |

墙钟 ≈ 最慢单请求 `total_ms`（全槽并行，非加总）。

### 8.2 流水线分段（Admin `image-timeline`，成功样本 P50）

| 阶段 | 10 并发 P50 | 占比 | 20 并发 P50 | 占比 |
|------|-------------|------|-------------|------|
| 排队合计（queue+psQueue+ssQueue+downloadQueue） | ~20ms | **0.1%** | ~22ms | **0.1%** |
| expand（pS） | ~6.8s | **27%** | ~15.2s | **47%** |
| SSE（sS） | ~12.0s | **47%** | ~11.7s | **36%** |
| download | ~1.2s | **5%** | ~1.0s | **3%** |
| 其他（账号选号/上传等） | ~1.0s | ~4% | ~1.0s | ~3% |
| **total** | **~25.5s** | | **~32.3s** | |

**瓶颈**：上游 expand + SSE 延迟（udeal 单线 SSE），非本地 `webGate` 或队列。排队始终 <100ms。

复现：`tools/analyze_conc_timing.py`、`tools/panda_image_conc_canary.py`（`GROK2API_GROUPS=10,20`）。

---

## 6. 生产变更清单（2026-07-27）

- 上传代理文件至 panda：`/root/.secrets/webshare20.txt`（20 住宅）、`/root/.secrets/webshare100_v2.txt`（100 机房）
- 新增 `tools/register_asset_nodes.py`（幂等注册脚本）
- 在 panda 执行注册 20 住宅代理为 `grok_web_asset` 节点
- 曾禁并恢复 udeal-111；asset 流量短暂中断后恢复
- **没有改动任何 Go 代码**，**没有部署新镜像**。生产仍运行 `sha256:dcb40f12…`（commit `cc95b65`）

---

> **下阶段启动条件**：新镜像部署 + 禁 udeal-111 生图仍 200 → 应用 web20 profile → 10/20 并发验收。

## 7. 代码修复（2026-07-27，待部署）

- `chrometicket_download.go`：住宅 egress 不复用 grok.com `cf_clearance`；asset rewarm 走 `ScopeWebAsset`
- `imagine_slots.go` + `WebPoolSnapshot.ticketReadyIds`（BE-024 只读）
- 工具：`register_asset_nodes.py`、`panda_apply_web20_profile.py`、`panda_verify_residential_asset.py`
