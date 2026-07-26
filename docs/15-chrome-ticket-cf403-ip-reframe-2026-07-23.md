# Chrome 票池与 CF403 / IP 漂移：认知重排（2026-07-23）

> 在 **有票路径**（`chrome_ticket_pool_hit`）下，跨 IP、跨 session、延迟用票均已实测可行。此前部分「CF403 / IP 漂移 / 封号」结论需按层重分类。

## 1. 失败分层（必须分开统计）

| 层级 | 现象 | 与票池关系 | 当前状态（2026-07-23） |
|------|------|------------|------------------------|
| L0 票池/签名 | 无 `pool_hit` | 无票或 meta 失效 | 池空时回退首页 meta |
| L1 Lite SSE | soft_stop / 1010 | 票有效但额度/上游拒绝 | 账号 imagine 额度 |
| L2 asset 下载 | `web_lite_asset_download_failed` 403 | **有票路径已修**（`c07cc2e`） | 无票路径仍可能 403 |
| L3 账号限速 | HTTP 429 `usage_limit` | 与票无关 | 单账号连打触发 |
| L4 出口 CF / 冷却 | egress 无节点、`cooldown_until` | **与票无关**；udeal 可用；Webshare 不可用 | 见 FAQ 下 |

**旧误区**：把 L2 403 一律归因于「开票 IP ≠ 消费 IP」。  
**新事实**：本机 Chrome 开票 + Panda udeal 消费，`pool_hit` 后 **200 + JPEG**。

### FAQ：udeal 冷却 vs 票跨 IP；Webshare 出图？

- **票**：`statsig_meta` + 设备 cookie，**不绑开票/消费 IP**（已测跨网）。
- **出口**：Panda 访问 grok.com 的代理。**udeal 冷却** = 该代理节点被系统暂时禁用（失败计数/cooldown），调度报「无可用 grok_web 出口」→ 503；**不是票过期**。
- **Webshare**：对 grok.com 为 CF challenge，生产 `grok_web` 上全关。**不能**用 Webshare 替代 udeal 做 Lite SSE；票跨 IP 也救不了 CF 403。

## 2. IP / 指纹绑定规则（实测更新）

| 绑定类型 | 是否绑定 | 证据 |
|----------|----------|------|
| 票 ↔ 开票公网 IP | **否** | 票内无 IP；S0/D-1/3/5m 跨网成功 |
| 票 ↔ 消费 egress IP | **弱绑定** | 换 udeal 节点未单独破坏有票路径 |
| 票 ↔ 开票设备 cookie | **是** | `grok_device_id` 等随票；换设备 cookie 未测 |
| 票 ↔ Playwright session | **否** | C1/C2 独立 subprocess 仍可 `pool_hit` |
| 池记录 ↔ 二次 pop | **是** | pop 即 consumed |
| meta 内容 ↔ 再入池 | **可** | R2 两次 `pool_hit`（当次 429 为额度） |

## 3. CF403 重新洗牌

### 仍成立

- **无票回退路径**：抓首页 meta + 无设备 cookie → asset 403 仍常见。
- **Webshare / 机房段**：Managed Challenge，不能作 `grok_web`。
- **握手与上传同 IP**：`grok.com` 请求须与 SSO 会话出口一致（见 doc 07）。

### 需降级/修正的表述

| 旧表述 | 修正 |
|--------|------|
| 「asset 403 = udeal 出口坏了」 | 有票 + 新下载链可 200；先查 `pool_hit` |
| 「必须同 IP 开票和消费」 | **票路径不需要**；无票路径仍依赖 egress warm |
| 「IP 漂移导致封号」 | 未见封号证据；见 429 额度与 soft_stop |
| 「每并发一个 Chrome」 | 瓶颈是 **池深 + SSE 槽 + 带宽**，非浏览器数 |

### 有票路径必要条件

1. `chrome_ticket_pool_hit`
2. `prepareChromeTicketDownloadCookie`（SSE 前 warm）
3. `resolveAssetDownloadCookie`（Python 风格下载头）
4. 账号 imagine 额度 > 0

## 4. 带宽与并发（295 额度语境）

| 资源 | 瓶颈？ | 说明 |
|------|--------|------|
| imagine 额度 | 曾瓶颈 | 池化后应轮换多账号；295 充足时非主因 |
| 单链路下行 ~15.5 Mbps | 下图可 6–8 并发 | 171KB JPEG ≈ 90ms/张 |
| 单链路上行 ~1.5 Mbps | 文生图 JSON 不是瓶颈 | 图生图上传才敏感 |
| SSE 同账号并发 | **固定 1** | 多账号才能真 10 并发 |
| 灌票 ~90s/张 | 池深不足时瓶颈 | `chrome_ticket_mint_fast.py` headless + 2–3 worker |

## 5. 实验与工具

| 工具 | 用途 |
|------|------|
| `chrome_ticket_survival_runner.py` | 单档存活（10m–12h），详细 probe |
| `chrome_ticket_survival_orchestrator.py` | 分档串行，出错即停 |
| `chrome_ticket_reuse_delay_runner.py` | 同 meta 再入池 + 间隔 |
| `chrome_ticket_validation_runner.py` | 5 轮串行 + 3×10 并发 |
| `_panda_image_probe.py` | Panda 侧：时间、egress、下载 Mbps |

日志：`.tmp/chrome-ticket-experiments.jsonl`

## 6. 运维建议

1. **生产生图**：维持池深 ≥ 并发×1.5；`chrome_ticket_mint_daemon.py` 或 `mint_fast`。
2. **排障顺序**：`pool_hit?` → `http` → `asset403` → `429` → egress。
3. **CF403 账号**：纳入验证集对比有票/无票，勿单独当「出口报废」。
4. **长龄存活**：10m–12h 分档独立票；3h+ 用 `--phase mint` 挂起后 `--phase consume`。

## 相关文档

- [13-chrome-ticket-pool-panda-api-2026-07-23.md](./13-chrome-ticket-pool-panda-api-2026-07-23.md)
- [14-chrome-ticket-lifecycle-experiments-2026-07-23.md](./14-chrome-ticket-lifecycle-experiments-2026-07-23.md)
- [12-web-lite-two-stage-failure-asset-403-2026-07-23.md](./12-web-lite-two-stage-failure-asset-403-2026-07-23.md)
