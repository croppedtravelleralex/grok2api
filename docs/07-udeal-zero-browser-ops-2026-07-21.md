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
- `providerWeb.mediaConcurrency=1`
- 本地签名器：宿主机 Python + `nsenter -t $(grok2api pid) -n` 共享容器 netns；脚本 `/tmp/zb_local_signer.py`，启动 `/tmp/start_signer_nsenter.sh`
- 算法：挑战链得 meta48+fp → 每请求 `METHOD!PATH!COUNTER + obfiowerehiring + fp` 现算票；**忽略** grok2api 传入的 HTML `metaContent`（HTML meta ≠ 签名 meta）
- 默认 `https://grok.wodf.de/sign` 从 panda 访问被 CF 挡，不可用

### 为何必须关 bridge

`applySignedStatsig`：若 `a.bridge != nil` 则 **直接 return**，不走 URL 签名器；`doModelRequest` 强制走 Chromium。Panda 资源与 bridge 会话不稳时 → anti-bot / 超时。纯 HTTP 路径 = 空 bridge URL + 本地签名器。

## 调度收窄（同日）

- **`grok_web` 启用 20 个**：原 10 号 `659,661,663,667,669,671,673,674,675,677` + 扩容 `641,642,644,646,647,649,650,652,654,656`；其余 web 号禁用
- 模型：`grok-imagine-image` / `quality` / `grok-chat-*` **开**；`imagine-image-edit` / `imagine-video` **关**
- NewAPI：渠道 `#105` 文生图 **status=1**；`#106` 编辑 / `#107` 视频保持 **status=2**
- abilities：`grok-imagine-image` / `quality` 挂 `#105`、`group=grok`；须 `enabled=true` 且 token 的 `"group"` 含 `grok`

### NewAPI 文生图验收（2026-07-21）

- **成功**：`POST http://127.0.0.1:8081/v1/images/generations`，model=`grok-imagine-image`，**HTTP 200**，约 **7–11s**（首次 ~10.7s，复测 ~7.4s），返回 `https://grokimage.relai.asia/v1/media/images/...`
- abilities：`#105` 的 `grok-imagine-image` / `quality` 已 `enabled=true`；`#106/#107` 保持 false
- **BaseURL（同日晚改）**：`#105` 改为容器内直连 **`http://grok2api:8000`**（避开公网 CF）。前提：`docker network connect grok2api_default new-api`（两 compose 项目默认不同网；**重建 new-api 后需重连**）
- 鉴权要点（本机 fork `new-api-local-rc16`）：
  - DB `tokens.key` **不存** `sk-` 前缀；客户端发 `Bearer sk-{key}`
  - key 体须为 **48 位无连字符**；带 `-` 的假 key（如 `sk-grok-web-...`）会被截成首段（日志里查 `key='grok'`）→ 401
  - token `"group"` 须能打到渠道 group（文生图用 **`grok`**）
- 可用探测：任意 `group=grok` 且 status=1 的 token（如 id `920`）；canary id `930`（user root / group `grok`）

## 带宽（单口压测）

| 指标 | 值 |
|------|-----|
| 下行串行 10MB | ~15.5 Mbps |
| 下行 4 并发合计 | ~16.5 Mbps（几乎不涨 → 单链路封顶） |
| 上行 5MB | ~1.54 Mbps（瓶颈） |

建议：`mediaConcurrency=1`；生图并行 ≤1；上传窗口 ~512KB；下载 1–2MB。扩吞吐靠 **多条住宅 sticky**，不要同请求换 IP。

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
