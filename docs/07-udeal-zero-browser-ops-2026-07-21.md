# Panda grok2api：udeal + 零浏览器签名（2026-07-21）

> 运维事实档。密钥不入库；代理口令只写在 panda 受控位置（egress 加密字段 / `/tmp` 脚本）。

## 结论摘要

| 项 | 状态 |
|----|------|
| LA 住宅出口 `70.39.164.200:30000`（用户称 udeal）过 CF | ✅ 可跑 grok.com 纯 HTTP |
| Webshare 扫 100 条 | ❌ 全 `cf_challenge`，不可作 `grok_web` |
| 零浏览器抽钥 + 现算 `x-statsig-id` | ✅ `tools/panda_zb_x45.py` |
| grok2api 生产 Web 对话 / 文生图 | ✅ 关 bridge + 本地签名器 + udeal egress |
| 图生图 / 视频 | ⏸ 模型已关；链路曾通但易撞额度冷却 |
| `cf_clearance` / 阶段混 IP | ❌ 不可：握手与上传同属 `grok.com`，IP 必须一致 |
| 代理组合正确切分 | `grok_web`=质量口；`grok_web_asset`=带宽口（CDN 下图） |
| udeal 旋转池 `as.udealproxy.com:6666` | ❌ **禁止**生产使用（见下「采购政策」） |

## 采购政策：禁止 udeal 旋转池

> **生效：2026-07-24。** 适用于 Panda 生产 `grok_web` / `grok_web_asset` 及一切入库前筛池。

| 规则 | 说明 |
|------|------|
| **禁止** | `as.udealproxy.com:6666` 旋转池；Desktop `udeal1000proxy.txt`；`session=` 粘滞批量转 HTTP 代理后入库 |
| **原因** | 节点单价高；历史 `pass_app` **2–4%**（80 条仅 3 条过 CF；40 条仅 1 条）；粘滞 session 会漂移变 `cf_challenge` |
| **允许** | 预验合格的 **固定粘滞住宅线**（当前：`70.39.164.200:30000` LA 口）；扩线须先 `tools/panda_zero_browser_http.py` → `probe_home` 过关再写 egress |
| **禁止** | 未筛旋转池批量写入 `egress_nodes`；Webshare 作 `grok_web`（100% challenge） |

验收门槛：`GET https://grok.com/` → 200 + `<title>Grok</title>`，无 `Just a moment`。

## 生产当前配置（panda `/opt/grok2api`）

### 出口

- `udeal-la-grok_web`（scope `grok_web`）— **唯一**启用的 Web API 出口
- `udeal-la-grok_web_asset`（scope `grok_web_asset`）— 下图
- 原 webshare `grok_web` / `grok_web_asset` 节点已禁用

### 签名 / 桥接

- `GROK2API_BROWSER_BRIDGE_URL=` **空**（compose 用 `${VAR-}` 而非 `${VAR:-default}`，空串才生效）
- `providerWeb.statsigMode=url`
- `providerWeb.statsigSignerURL=http://127.0.0.1:8788/sign`
- `providerWeb.mediaConcurrency=1`（视频）
- `providerWeb.webConcurrency=2`（chat/Lite；单 sticky 折中）
- `providerWeb.assetConcurrency=8`（CDN 下图）
- 本地签名器：宿主机 Python + `nsenter -t $(grok2api pid) -n` 共享容器 netns；脚本 `/tmp/zb_local_signer.py`，启动 `/tmp/start_signer_nsenter.sh`
- 算法：挑战链得 meta48+fp → 每请求 `METHOD!PATH!COUNTER + obfiowerehiring + fp` 现算票；**忽略** grok2api 传入的 HTML `metaContent`（HTML meta ≠ 签名 meta）
- 默认 `https://grok.wodf.de/sign` 从 panda 访问被 CF 挡，不可用

### 为何必须关 bridge

`applySignedStatsig`：若 `a.bridge != nil` 则 **直接 return**，不走 URL 签名器；`doModelRequest` 强制走 Chromium。Panda 资源与 bridge 会话不稳时 → anti-bot / 超时。纯 HTTP 路径 = 空 bridge URL + 本地签名器。

## 调度收窄（同日）

- **`grok_web` 启用 20 个**：原 10 号 `659,661,663,667,669,671,673,674,675,677` + 扩容 `641,642,644,646,647,649,650,652,654,656`；其余 web 号禁用
- 模型：`grok-imagine-image` / `quality` / `grok-chat-*` **开**；`imagine-image-edit` / `imagine-video` **关**
- NewAPI：渠道 `#105` 文生图测试期 **status=2（关）**；`#106` 编辑 / `#107` 视频保持 **status=2**
- abilities：`grok-imagine-image` / `quality` 挂 `#105`、`group=grok`；重开时须 `enabled=true` 且 token 的 `"group"` 含 `grok`

### NewAPI 文生图验收（2026-07-21）

- **成功**：`POST http://127.0.0.1:8081/v1/images/generations`，model=`grok-imagine-image`，**HTTP 200**，约 **7–11s**（首次 ~10.7s，复测 ~7.4s），返回 `https://grokimage.relai.asia/v1/media/images/...`
- abilities：`#105` 的 `grok-imagine-image` / `quality` 已 `enabled=true`；`#106/#107` 保持 false
- **BaseURL（同日晚改）**：`#105` 改为容器内直连 **`http://grok2api:8000`**（避开公网 CF）。前提：`docker network connect grok2api_default new-api`（两 compose 项目默认不同网；**重建 new-api 后需重连**）
- 鉴权要点（本机 fork `new-api-local-rc16`）：
  - DB `tokens.key` **不存** `sk-` 前缀；客户端发 `Bearer sk-{key}`
  - key 体须为 **48 位无连字符**；带 `-` 的假 key（如 `sk-grok-web-...`）会被截成首段（日志里查 `key='grok'`）→ 401
  - token `"group"` 须能打到渠道 group（文生图用 **`grok`**）
- 可用探测：任意 `group=grok` 且 status=1 的 token（如 id `920`）；canary id `930`（user root / group `grok`）

## 带宽与文生图载荷（单口压测 + 生产样本）

> **流量统计更新：2026-07-24。** 原始数据：`/tmp/g2a-udeal-bw.json`（`bw_udeal.sh`）；`tools/_panda_img_bw_stats.py`；`media_assets` 表。

### 代理链路压测（07-21，LA `70.39.164.200:30000`）

| 项目 | 方法 | 结果 |
|------|------|------|
| 下行串行 10MB ×3 | curl via proxy → Cloudflare `__down` | median **15.5 Mbps**（15.27–15.60） |
| 下行 4 并发 ×5MB | 4 线程同时 curl | 合计 **16.5 Mbps**（单链路封顶，并发几乎不涨） |
| 上行 5MB | curl POST → httpbin | **1.54 Mbps**（瓶颈） |

### 生产出图体积（`media_assets`，kind=image）

| 样本 | n | mean | p50 | min–max | 分辨率 |
|------|---|------|-----|---------|--------|
| 07-21 首测 | 34 | 170 KB | 153 KB | — | 784×1168 |
| **07-24 复测** | 200 | **131 KB** | **129 KB** | 90–198 KB | 784×1168 / 1168×784 |

**累计存储（≈ 出图下行流量下界）：**

| 窗口 | 张数 | 总流量 |
|------|------|--------|
| 全量 | 306 | **41.8 MB** |
| 近 24h | 179 | **22.9 MB** |

生图墙钟（`generation_duration_ms`，n=200）：mean **9.8s**，p50 **9.1s**。

### 单次 probe 带宽字段（工具已有，实验未落盘）

`tools/_panda_image_probe.py` 在 API 200 后对返回 URL 测 `download_bytes` / `download_ms` / `download_mbps`。  
`.tmp/chrome-ticket-experiments.jsonl` 中 **`download_mbps` 样本 = 0**（裸 GET media URL 常 403；实验只记了 `wall_ms`）。  
**V-conc 3×10**、**M2 双并发** 带宽验证 **未跑完**。

### 未统计 / 缺口

| 缺项 | 说明 |
|------|------|
| udeal 供应商账单流量 | 未接 API |
| SSE 上行字节 | 估算 KB 级，未逐请求计量 |
| 出口实时吞吐 | 无 vnstat / 代理 access log |
| 10 并发生产带宽 | 仅链路压测有数据 |

### 推算（单张文生图 @ LA 口）

| 阶段 | 流量 | 带宽占比 |
|------|------|---------|
| SSE 上行 JSON | 几 KB | 可忽略 |
| 下图 ~130 KB JPEG | ~0.07s @ 15.5 Mbps | 在 9s 墙钟里极小 |
| 图生图上传（若开） | base64 ~230 KB | ~1.2s @ 1.54 Mbps |

**结论：带宽不是当前瓶颈**；瓶颈是上游 SSE 墙钟 + 单出口 + 账号池。日均出图 ~180 张 ≈ **23 MB** 量级（不含探针/重试）。

| 指标 | 值 |
|------|-----|
| 下行串行 10MB | ~15.5 Mbps |
| 下行 4 并发合计 | ~16.5 Mbps（几乎不涨 → 单链路封顶） |
| 上行 5MB | ~1.54 Mbps（瓶颈） |
| Lite 出图体积 | mean **~131KB** / p50 **~129KB** JPEG |
| 分辨率 | **784×1168**（或对调，约 1K 竖/横图） |

时间估算（单口）：

| 动作 | 估算 |
|------|------|
| 下图 171KB @ 15.5 Mbps | ≈ **90ms** → 下图并发可开到 6–8，几乎不吃带宽 |
| 文生图上行 | 仅 JSON（KB 级），**不是**上行瓶颈 |
| 图生图上传（同体积 base64≈228KB）@ 1.54 Mbps | ≈ **1.2s** → 同口上传应串行；当前编辑模型已关 |
| 文生图墙钟 8–20s | 主要在上游 SSE / soft_stop，**不是**带宽 |

调度建议（单条 udeal sticky）：

| 闸门 | 建议 | 说明 |
|------|------|------|
| `webConcurrency` | **2**（流水线后建议逐步升到 **8**） | ScopeWeb：chat / Lite Drawing SSE / 刷额度；扩写已拆到 expandGate |
| `assetConcurrency` | **8** | ScopeWebAsset：CDN 下图；171KB×8 ≪ 15Mbps 链路 |
| `expandConcurrency` | **2** | ScopeWebExpand：短 prompt 扩写；节点回退 grok_web，同账号 sticky |
| `mediaConcurrency` | **1** | 仅视频 worker，与文生图无关 |

### 文生图流水线与时序图（同日晚）

- 同步 `/v1/images/generations` 不变；Lite 走 `ImagePipelineScheduler`（固定 10 槽、准入队列 100、扩写池 2、SSE AIMD 2–6 + 错峰、下图池对齐 asset=8）。
- 账号 lease **在 SSE 出 URL 后早释**，下图不再占账号并发。
- 管理端：`GET /api/admin/v1/image-timeline?window=30m|1h|6h|12h`；前端左侧「时序图」`/image-timeline` 实时甘特（排队/扩写/SSE/下图分色）。
- Canary：`GROK2API_GROUPS=2,4,10 python tools/panda_image_conc_canary.py`（输出 P50/P90）。
- Quality/WS 路径暂不纳入流水线。

说明：代码侧上传是 **整图 base64 一次 POST**（无独立「512KB 上传窗口」旋钮）；下载 `ReadAll` 上限 32MB，对 ~170KB JPEG 无需再砍窗口。扩吞吐靠 **多条住宅 sticky**，不要同请求换 IP。

## 代理组合（设计结论）

不可行：`udeal` 握手 + `webshare` 上传（上传仍打 `grok.com`，CF 绑 IP）。

可行：

1. `grok_web` → udeal（API/上传/生图）
2. `grok_web_asset` → webshare 或高带宽（仅 CDN `assets.grok.com` 等，需单独 canary）
3. 多 udeal sticky 分账号并行

代码已分 scope：`downloadImage` → `ScopeWebAsset`；chat/upload/imagine → `ScopeWeb`。

## 出口冷却算法与统计（2026-07-24）

> 数据源：Panda `backend.db` `egress_nodes` / `request_audits`；`docker logs grok2api --since 72h`；本机 `.tmp/chrome-ticket-experiments.jsonl`（54 条）。  
> 代码：`backend/internal/infra/egress/manager.go` → `FeedbackForScope`。

### 冷却算法（grok2api 自管，与票无关）

| 结果 | 行为 |
|------|------|
| 2xx–3xx | `failure_count=0`，清 `cooldown_until`，health↑ |
| **401 / 429** | **忽略**，不污染出口 |
| **403** | `failure_count++`，health×0.7，**不设** `cooldown_until` |
| 5xx / 传输错误 / 其他 | `failure_count++`，进入指数冷却（**2026-07-25 起**：`routing.disableCooldown=true` 时 **不写** `cooldown_until`） |

| 连续失败次数（disableCooldown=false 时） | 冷却时长 |
|--------------------------|---------|
| **1** | **30s** |
| 2 | 60s |
| 3 | 2min |
| 4 | 4min |
| ≥5 | **10min**（封顶） |

**要点（历史）**：第 1 次可计数失败即冷却。`downloadImage` 的 asset 403 **不调** `Feedback`。

当前生产仅 **node 110**（`grok_web`）启用。`disableCooldown=true` 时一般不再因冷却出现「无 grok_web 出口」；若仍 503 则查节点 enabled/health 或单节点故障。

### 生产环境基线

| 参数 | 值 |
|------|-----|
| 出口 | `udeal-la-grok_web`（110）、`udeal-la-grok_web_asset`（111），同 IP `70.39.164.200:30000` |
| `WebConcurrency` | **2**（SSE/chat 闸门） |
| `AssetConcurrency` | **8** |
| `ExpandConcurrency` | **2** |
| 流水线槽位 | 10（受 `WebConcurrency` 限制） |
| 启用 Web 账号 | **20**（全粘滞同一出口） |
| 维护探针 | ~15s 一轮（`DispatchInterval`） |

### 72h docker 日志分类

| 类型 | 次数 | 触发 egress 冷却？ |
|------|------|-------------------|
| `maintenance_probe_failed` 429 | **1749** | 否（acct 88 连打为主） |
| `maintenance_probe_failed` 401 | 772 | 否 |
| `maintenance_probe_failed` egress 不可用 | **2** | 症状 |
| `dispatch_probe_failed` soft_stop / render | **17** | 可能（见下 12:10 事件） |
| `dispatch_probe_failed` egress 不可用 | **1** | 症状 |
| `web_lite_image_not_found` (soft_stop) | 21 | 否 |
| `asset_download_failed` 403 | 12 | 否（acct 1467/92 各 6） |
| `egress_unavailable` 文案总计 | **10** | — |
| `image_upstream_failed` → egress | **1** | 是 |
| `image_upstream_failed` → 无可用账号 | 36 | 否（账号池） |
| `image_upstream_failed` → 账号冷却 | 7 | 否 |

### 48h `request_audits`（grok_web / image）

| status | 次数 | 备注 |
|--------|------|------|
| 200 | 266 | 24h 成功 44 次，墙钟 avg **~9.8s**（7.6–14.6s） |
| 503 | 124 | **绝大多数为账号池**，非 egress |
| 429 | 26 | Imagine 限速 |
| 403 | 4 | — |

**07-24 实验时段（01–05 UTC+8 连续小时）：** 36 次 200、0 次 503 → 单口在串行/低并发下稳定。

### 有记录的 egress 冷却事件（07-24 12:10）

```
12:10:34  dispatch_probe acct=493  「连接失败: Some content couldn't be rendered.」
12:10:38  maintenance_probe       「当前没有可用的 grok_web 出口节点」
12:10:49  dispatch_probe            同上
12:10:53  maintenance_probe       同上
12:27:09  生图 API 200             ← 冷却后恢复
```

→ 约 **1 次上游失败 → 30s 冷却**（与算法一致）。当日 06:14 实验曾人工清 110/111 的 `cooldown_until`（`.tmp/_fix_egress.py`）。

### 票实验 jsonl（54 条，本机 Chrome 开票 → Panda 消费）

| 指标 | 值 |
|------|-----|
| http=200 + pool_hit | **20** |
| http=429 + pool_hit | **8**（票有效，账号限速） |
| http=503 + pool_hit | **6**（票命中，**账号池**空） |
| http=503 无 pool_hit | **6** |
| http=502 + pool_hit | **1**（V-serial-5） |

| 批次 | 并发 | 结果 |
|------|------|------|
| V-serial 1–4 | 1 | pass（中间偶发账号 503，重试后 200） |
| V-serial-5 | 1 | 502+pool_hit |
| S-10m / 30m / 60m | 1 | pass |
| R-delay 0/1m/5m/30m | 1（每档双 probe） | 全 pass |
| M2 | **2** | 全 429，未验带宽 |
| V-conc 3×10 | — | 未跑 |

### 503 排障分层（必读）

| 错误文案 | 层 | 处理 |
|---------|-----|------|
| `当前没有可用的 grok_web 出口节点` | **出口冷却** | 等 30s–10min 或清 `egress_nodes.cooldown_until` |
| `没有可用上游账号` | **账号调度** | pin 须在图池 / 重启 grok2api / 查 lease |
| `可用上游账号正在冷却` | **账号冷却** | 换号或等账号 `cooldown_until` |
| 429 code8 / usage_limit | **Imagine 限速** | 换号；**不**清出口 |

### Webshare 历史（已禁用，验证算法）

`failure_count` 分布：0×16、1×9、2×5、3–5 各 1、13×1、17×1；`last_error` 均为 `transport error`。多次失败后冷却仍封顶 10min。

## 脚本与证据

| 路径 | 用途 |
|------|------|
| `tools/panda_zb_x45.py` | 零浏览器 e2e |
| `/tmp/zb_local_signer.py` | grok2api 兼容 `/sign` |
| `/tmp/start_signer_nsenter.sh` | 随 grok2api 重启后拉签名器 |
| `/tmp/sched_udeal20_text2img_only.sh` | 20 号调度 + 关编辑 |
| `/tmp/pool20_baseurl.json` | BaseURL + 20 号启用证据 |
| `/tmp/g2a-udeal-bw.json` | 带宽结果 |
| `/tmp/g2a-udeal-quota10b.json` | 10 号刷额度 10/10 |
| `.tmp/pure-http-grok/` | 本机矩阵证据 |

## 运维注意

1. `docker compose` 重建 `grok2api` 后必须重跑签名器（netns PID 变了）
2. egress 健康过低时删除重建节点比 toggle 更干净
3. `refresh-quotas` 不接受 ids；用「只 enable 目标号」隔离
4. 账号列表 `limit` 无效、固定 pageSize=20，翻页用 `page=`
5. 正式部署仍走 git/GHCR；本轮为 panda 受控 `.env`/egress/设置热改，未改业务镜像
6. **禁止** udeal 旋转池入库；扩出口只加预验合格的固定粘滞线
7. 503 先分清「出口冷却」vs「账号池」再动手（见上表）
