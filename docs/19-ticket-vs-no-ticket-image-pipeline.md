# 无开票 vs 开票生图链路深入对比（2026-07-24）

> 对照对象：**Panda 生产 grok2api** 两条路径 —— 无 `pool_hit` 的纯 HTTP 回退链 vs Chrome 票池 `pool_hit` 链。

## 1. 架构对照

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ 公共段（两条路径相同）                                                    │
│  Client → grok2api pipeline → Selector 选号 → egress.Acquire(udeal)      │
│  → grok-signer 现签 x-statsig-id（~45s）→ openChat/SSE Lite            │
└─────────────────────────────────────────────────────────────────────────┘

路径 A — 无开票（回退）
  statsig_meta 来源：egress GET grok.com/ 首页 HTML 提取（fetchStatsigMetaContent）
  设备 cookie：仅 egress warmed cf_clearance + SSO；无 grok_device_id 缓冲
  代码：statsig.go metaContent() 无 metaOverride → 每次请求可能 refresh 首页 meta
  下载：chrometicket_download 不介入；asset 走 egress warm 常规 cookie

路径 B — 开票（pool_hit）
  前置：attachChromeTicket → PopForAccount(account_id) → consumed
  statsig_meta 来源：池内 Chrome 预捕获（12h TTL）→ Sign(metaOverride)
  设备 cookie：票内 grok_device_id + x-userid；prepareChromeTicketDownloadCookie
  下载：SSE 前 CF warm + Python 对齐下载头（c07cc2e）
```

| 维度 | 路径 A 无开票 | 路径 B 开票 |
|------|---------------|-------------|
| **meta 来源** | Panda egress 实时抓首页 | 本机 Chrome 批量捕获入池 |
| **短票 x-statsig-id** | grok-signer 每次现签 | 同左（用池内 meta 作 signer 输入） |
| **设备指纹** | 无/弱（仅 egress jar） | 票携带 grok_device_id |
| **cf_clearance** | egress warm 写入 | 仍由 egress warm；票**不含** cf |
| **IP** | udeal 出口 | 同左；开票 IP ≠ 消费 IP 已验收 |
| **Panda 上 Chrome** | 0 | 0（灌票在本机） |
| **每图额外成本** | 1× 首页 meta fetch（若 cache miss） | 1× 票 pop（预付费 ~90s Chrome/张） |

## 2. 代码锚点

| 环节 | 无开票 | 开票 |
|------|--------|------|
| 取票 | `attachChromeTicket` 无票 → 原 ctx | `chrometicket.go` PopForAccount |
| meta 注入 | `statsig.go` `metaOverride==""` | `LeaseFromContext` → `ticket.StatsigMeta` |
| 生图入口 | `image.go` `generateLiteImageURL` | 先 `attachChromeTicket` 再 `openChat` |
| asset 下载 | 标准 egress cookie | `chrometicket_download.go` warm + merge device cookie |
| 可观测 | 无 pool_hit 日志 | `chrome_ticket_pool_hit` / `chrome_ticket_popped` |

## 3. 分阶段成功率（有证据）

| 阶段 | 无开票 | 开票 (pool_hit) | 证据 |
|------|--------|-----------------|------|
| L0 调度 503 | Phase B 后 **0%** | 同左 | 2026-07-24 BE-019 |
| L1 CF / 上游握手 | udeal 下旧 403→可连 | 同左 | doc 07 |
| L2 SSE 开图 | 可有 200 | 可有 200 | 票实验 |
| L3 **asset 下载** | bench **0/5** 403 | **c07cc2e 后 200** | doc 12、13 |
| L4 账号 429 | 高频 | 同左（票不能修） | bench 200 轮 196×429 |
| L5 soft_stop 502 | 常见 | 同左 | Phase B |

**关键差异集中在 L2→L3**：无票时 SSE 成功仍常死于 `assets.grok.com` 403；有票 + warm 可把完整 E2E 打通。

## 4. 实测快照（2026-07-24）

### 4.1 无开票倾向（池空或账号无票）

| 测试 | n | HTTP 200 | 备注 |
|------|---|----------|------|
| Phase B smoke | 10 | 0 | 6×429, 4×502 |
| 无票 asset bench | 5 | 0 | 阶段 2 全 403 |
| 本机 pure HTTP PoC（7897） | 2 | 0 | meta OK，chat anti-bot 403 |

### 4.2 开票路径（pool_hit）

| 测试 | n | HTTP 200 | pool_hit |
|------|---|----------|----------|
| S0 + D-1/3/5m | 4 | 4 | 4/4 |
| R-delay 全档 | 8 | 8 | 8/8 |
| serial bench suite 1 | 10 | **3** | 有票但后 7×429 |
| 200 轮吃票 bench | 200 | 3 | 额度耗尽后全 429 |

### 4.3 本机 PoC vs Panda 生产

| 环境 | meta 提取 | chat | 说明 |
|------|-----------|------|------|
| 本机 127.0.0.1:7897 | ✅ 100% | ❌ anti-bot | 住宅代理无 udeal 会话形态 |
| Panda udeal | 待 PoC 深化 | 待测 | `pure_http_statsig_meta_poc.py --proxy udeal` |

## 5. 与历史「无浏览器纯 HTTP」PoC 的关系

| 代际 | 入口 | 浏览器 | 与当前生产关系 |
|------|------|--------|----------------|
| v0 browser-bridge | 每请求 Chrome | 常驻 | 已废弃（内存/CF 成本） |
| v1 本机 Chrome 短签 + curl | `web_http_chat_image_canary` | 仅出票 | PoC，未上 grok2api |
| v2 Panda signer + udeal | `panda_zero_browser_http.py` | 0 | **≈ 路径 A**（无票池） |
| v3 Chrome 票池 + signer | 生产 grok2api | 灌票在本机 | **路径 B** |

路径 A 是 v2 的生产化：`statsigMode=url` + sidecar signer + egress 抓 meta。  
路径 B 在 A 之上增加 **预捕获 meta/设备 cookie 缓冲层**，修 L3 asset。

## 6. 决策矩阵

| 目标 | 推荐路径 |
|------|----------|
| Panda 零 Chrome、能接受 asset 403 风险 | A（无票）— **当前不推荐生产 E2E** |
| 稳定 Lite 两阶段 E2E | **B（开票）** + udeal + 额度/四池 |
| 去掉本机 Chrome 灌票 | 需 PoC：curl_cffi 稳定 meta+fp（见 doc 18）— **未就绪** |
| 提高整体成功率 | **四池调度 + 额度**，不是多灌票 |

## 7. 相关文档

- [12-web-lite-two-stage-failure-asset-403](./12-web-lite-two-stage-failure-asset-403-2026-07-23.md)
- [13-chrome-ticket-pool-panda-api](./13-chrome-ticket-pool-panda-api-2026-07-23.md)
- [17-web-four-pool-and-imaging-success-rates](./17-web-four-pool-and-imaging-success-rates-2026-07-24.md)
- [18-pure-http-statsig-meta-poc](./18-pure-http-statsig-meta-poc.md)
- [http-reverse-lite-chain](./http-reverse-lite-chain.md)
