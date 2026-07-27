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

### 3.2 100 条升级机房线已上传但未注册

上传至 panda `/root/.secrets/webshare100_v2.txt`。

### 3.3 asset affinity 首次验证结果

注册后禁掉原先唯一的 asset 节点 udeal-111（node 111），生图全部失败（asset 403）。
恢复 udeal-111 后正常。**住宅节点需要先排查 affinity 路由问题**才能独立承载流量。

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

| P | 事项 | 预估 |
|---|------|------|
| **P0** | **排查 asset affinity 哈希环**：为何住宅节点注册后没有被分配下载流量。查 `web/asset` 选节点代码 | 1 次部署 |
| P0 | 分阶段抬 `webConcurrency` 2→8→20，同步扩槽位 | 3 发布周期 |
| P1 | 恢复 `grok2api-web-clearance.service`（当前 failed） | 半小时 |
| P2 | 注册 100 升级机房线为备用 asset 节点 | 1 次脚本 |
| P2 | `rewarmAssetDownloadCookie` deviceCookie 前置修复 | 1 次部署 |
| P2 | `image_pipeline_traces` 断写修复 | 待定 |
| P2 | 维护探针死号隔离（dead lane 退避） | 1 次部署 |

---

## 6. 生产变更清单（2026-07-27）

- 上传代理文件至 panda：`/root/.secrets/webshare20.txt`（20 住宅）、`/root/.secrets/webshare100_v2.txt`（100 机房）
- 新增 `tools/register_asset_nodes.py`（幂等注册脚本）
- 在 panda 执行注册 20 住宅代理为 `grok_web_asset` 节点
- 曾禁并恢复 udeal-111；asset 流量短暂中断后恢复
- **没有改动任何 Go 代码**，**没有部署新镜像**。生产仍运行 `sha256:dcb40f12…`（commit `cc95b65`）

---

> **下阶段启动条件**：asset affinity 排查通过，住宅节点能独立承载 asset 下载流量。
