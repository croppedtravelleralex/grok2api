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

| 指标 | 值 |
|------|-----|
| 下行串行 10MB | ~15.5 Mbps |
| 下行 4 并发合计 | ~16.5 Mbps（几乎不涨 → 单链路封顶） |
| 上行 5MB | ~1.54 Mbps（瓶颈） |
| Lite 出图体积（`media_assets` n=34） | mean **170KB** / p50 **153KB** / 样例 **171.35KB** JPEG |
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
