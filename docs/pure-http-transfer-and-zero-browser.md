# 纯 HTTP 分层：票 / 钥 / CF / 零浏览器

> 证据：`tools/pure_http_transfer_matrix.py` 实测（2026-07-21），见 `.tmp/pure-http-grok/transfer_matrix_result.json`。

## 1. 用的是「票」还是「钥」？

| 物件 | 寿命 | 请求时是否复用浏览器产物 |
|------|------|--------------------------|
| `x-statsig-id`（票） | 计数器约 **当天级窗口**（10min 旧票仍 200；**1 天旧票 → anti_bot**） | **否**：每次 Python `generate_statsig()` **现算新票** |
| `meta48` / `fingerprint`（钥） | 远长于票（会话/脚本版本级） | **是**：从一次抽钥得到，之后复用 |
| `cf_clearance` | 绑定出口 IP | 好出口下 **可不带**（见下） |

结论：之前的 pass **不是**「浏览器短命票还没过期」。
矩阵 A/B/C：`fresh`/`stale_10min`=chat_ok，`ancient_1day`=anti_bot；且每次 `sig_n` 递增、前缀不同。

## 2. 抽钥 / CF 能否转移？不挑代理？

### 可转移（跨时间、跨机器，只要算法一致）

- SSO
- `meta_b64`（= HTML `grok-site*verification` 的 48 字节）
- `fingerprint`（SVG 动画派生；社区 XCTID：`tools/xctid_fingerprint.py`）
- 签名算法本身

### 不可随意转移

- **`cf_clearance`：绑定 IP**。panda 直连 `43.156.233.219` → `cf-mitigated: challenge`，还没走到 statsig。
- 本机 **好出口（Mihomo）** 上：`D_no_cf_clearance` / `E_sso_only` 均为 **chat_ok** → 好出口下 CF 可省略。

### 「不挑代理」现实结论

| 出口类型 | 要什么 |
|----------|--------|
| 干净住宅 / 已过 CF 的 VPN 节点 | SSO + 现算票即可（本矩阵已证；**panda→LA 住宅**亦证） |
| 机房 / 脏 IP（panda 直连、Webshare 扫过的段） | **每出口**过 CF（浏览器或 CF solver），**不能**指望转移旧 `cf_clearance` |

「不挑代理」≠「CF cookie 全球通用」；只能做到：**签名层不挑代理**，CF 层按出口现解或只跑干净池。

**已证转移（2026-07-21 panda）**：本机抽出的 `meta+fp` + SSO，经 `70.39.164.200` 住宅出口：

- `P1` SSO-only 对话 `chat_ok`
- `P5` Lite 生图 `image_ok`
- `P0` 同钥 panda 直连 → `cf_challenge`（CF 不可跨脏 IP）
- `P3` 用首页 HTML verification 当 meta → `anti_bot`（**HTML meta ≠ 签名 meta**）

## 3. 零浏览器 / 时空出口无限复用 — 目标拆解

```
L3 签名  ✅ 纯 Python 现算（已通）
L2 抽钥  ◐ HTML verification 可 curl 拉；fp 需 SVG+x_values（XCTID 已移植，缺 HTTP 挑战链）
L1 CF    ❌ 不能跨 IP 无限复用；脏出口必须现解
```

### 已实现

- `tools/pure_http_grok_runtime.py`：请求时无 Chrome，Python 现算票
- `tools/pure_http_transfer_matrix.py`：票龄 / CF / SSO 矩阵
- `tools/xctid_fingerprint.py`：SVG→fingerprint→票（输入齐即可）

### 未完成（代理恢复后继续）

1. 纯 HTTP 拉首页 verification，对齐 `meta_b64`（脚本：`tools/pure_http_zero_browser_bootstrap.py`；本轮被本机 Mihomo TLS 上游中断）
2. 复现 Grok-Api Phase2：`c_request` 拿 SVG / `x_values`，彻底去掉一次 Chrome 抽 fp
3. 脏出口：接 CF managed challenge solver（或强制干净住宅池），**不要**假装 CF 可转移

### 可验证 AC

- [x] 现算票 ≠ 复用浏览器票（矩阵 A/B/C）
- [x] 好出口无 CF 可对话（D/E）
- [x] 脏出口 CF 挡在 L1（panda 403 challenge）
- [x] **钥可转移**：本机 Chrome 抽出的 meta+fp → panda + LA 住宅 `70.39.164.200`，仅 SSO → chat_ok + Lite image_ok（`P1`/`P5`）
- [x] **HTML verification ≠ 签名用 meta**（`meta_eq_extracted: false`；`P3` 用 HTML meta → anti_bot）
- [x] **零浏览器（历史基线）**：挑战 token + curves/SVG + 当时的 `x_values=[45,42,40,32]`，panda+住宅出口文本+Lite 通（`tools/panda_zb_x45.py`）；x-values 属于前端构建资产，不能永久硬编码。
- [x] **Webshare 扫池**：panda `tools/panda_zb_webshare_scan.py` 100/100 `cf_challenge`，不可作 `grok_web`
- [x] **grok2api 生产接入（2026-07-21）**：关 browser-bridge + 本地 `/sign`（忽略 HTML meta）+ udeal-only egress → Web 对话/文生图 200；详见 [07-udeal-zero-browser-ops-2026-07-21.md](./07-udeal-zero-browser-ops-2026-07-21.md)
- [x] **代理不可阶段混 IP**：上传仍打 `grok.com`，不能 udeal 握手 + webshare 上传；asset CDN 才可另挂高带宽口
- [x] **调度收窄**：仅 udeal 托管 10 号进 `grok_web`；只开文生图，edit/video 关
- [x] **NewAPI `#105` 文生图 e2e**：`8081/v1/images/generations` → 200 + `grokimage.relai.asia` 媒体 URL
- [ ] 脏出口纯 HTTP 解 CF；`grok_web_asset` 挂 webshare 下图 canary

## 4. 2026-07-22 生产化范围

本轮“纯 HTTP”特指 `grok-imagine-image` 的 Lite 文生图主路径：请求时不启动浏览器，由本地 signer 为每个上游请求现算 `x-statsig-id`，应用只缓存首页/挑战元数据，不缓存可直接回放的票。以下能力不在本轮 10 并发验收范围内：

| 能力 | 本轮状态 | 原因 |
|------|----------|------|
| Lite 文生图 | 已接入纯 HTTP 流水线 | REST Expand + SSE + CDN 下载链路已实现 |
| Quality 生图 | 不纳入 | 走 Imagine WebSocket，协议与容量模型不同 |
| 图生图/Edit | 保持关闭 | 上传、额度和冷却策略未完成生产验收 |
| 视频 | 保持关闭 | 独立异步任务，不复用图片流水线 |
| 脏出口解 CF | 未实现 | `cf_clearance` 仍与出口绑定 |

生产链路为：

```text
客户端并发请求
  → Admission（10 槽，等待队列 100，满则本地 429）
  → Expand FIFO（2）
  → 账号选择（同账号 Lite 生图并发固定为 1）
  → SSE FIFO/AIMD（target 从 1 开始，最大 6）
  → SSE 结束即提前释放账号 lease
  → Download FIFO（8）
  → 媒体落盘/返回 URL
```

“支持 10 并发”的准确含义是：10 个客户端请求可同时进入 10 个流水线槽，并在 Expand/SSE/Download 三层各自排队；它不等于让单一住宅出口同时硬冲 10 条 SSE。单出口的上行最敏感，因此 SSE 从 1 安全上探；下载走独立 8 并发，不占账号 lease。

## 5. 票、钥、CF 在生产中的职责

| 层 | 当前实现 | 失效表现 | 恢复动作 |
|----|----------|----------|----------|
| 票：`x-statsig-id` | 每请求按 method/path/当前 counter 现算 | code 7 / anti-bot 403 | 先验证 matched seed+HEX，不能靠重放旧票 |
| 钥：48-byte seed + fingerprint/HEX | signer 进程内加载；可跨机器/时间转移 | signer health 仍 200，但所有账号在 4–6s 内 403 | 从当前前端捕获真实 digest，更新 matched pair |
| CF/出口 | 住宅出口保持一致 | `cf-mitigated: challenge` 或连接失败 | 换干净出口或按出口解 CF |
| SSO/账号 | 每次选择不同账号；额度状态模型级持久化 | `usage_limit_reached` 429、SSE soft-stop | 模型级隔离/降权，不污染账号其他模型 |

本轮再次证明 signer `/healthz` 只能证明进程可用，不能证明 seed+HEX 被 Grok 接受。生产 canary 必须包含一次真实 `/v1/images/generations`。

## 6. “两个账号同时生图 429”的根因结论

用户观察到的“429”实际混合了三种不同故障：

1. **本地流水线 429**：10 个槽和 100 等待位都满，返回 `Retry-After`；本轮未触发。
2. **上游真实 429**：响应码 `usage_limit_reached`，表示该账号的 Imagine 模型额度/窗口不可用；会写入模型级 quota block，不影响聊天模型。
3. **SSE soft-stop**：HTTP 会话可建立，但最终帧 `isSoftStop=true` 且没有图片；旧路径对外常表现为 502，不是真实 HTTP 429。

生产证据排除了“必须两个账号并发才触发”的假设：单请求也多次出现 soft-stop 或 `usage_limit_reached`。同时还发现一个独立并发缺陷：近期成功账号优先后，两条并发请求曾同时租到同一账号，其中一路被上游返回 `You have too many requests in progress`。提交 `52c04ec` 将 Lite 生图的账号并发固定为 1；生产复验中两路请求已使用不同账号，该错误不再出现。

账号调度策略现为：同模型近期成功账号优先、未知账号其次、soft-stop 账号最后；soft-stop 冷却从 30 秒指数增加至 5 分钟，成功偏好保留 30 分钟。Lite soft-stop 最多换 6 个不同账号并采用 1/2/4 秒退避；显式 429 仍受原始 retry budget 控制并尊重额度冷却。

## 7. signer 漂移与本轮恢复

- 旧前端索引：`[31,16,43,8]`。
- 2026-07-22 静态模块 `4629918 → 11l7ylw6aaq_g.js → 1645e3` 的当前索引：`[38,33,24,32]`。
- 仅更新索引后曾恢复生产请求，但 8 分钟动态 challenge 刷新随后生成了服务端不接受的 pair；表现为 signer health 正常、所有账号 code 7。
- 通过一次性 Chrome digest 捕获获得当前真实 matched seed+HEX；关闭浏览器后，纯 HTTP 文本 200（0.88s）和 Lite 200（5.74s），证明请求期仍无浏览器。
- Panda 只同步 seed+HEX，不同步抽钥账号的 SSO/Cookie；动态自动重算窗口临时放宽到 24 小时，避免已证伪的 8 分钟刷新覆盖有效 pair。

当前 signer 仍是 Panda `/tmp` 下的宿主机 Python + `nsenter` 进程，不属于镜像/sidecar；`start_signer_nsenter.sh` 内的 `pkill -f` 也可能误杀包含脚本文件名的 SSH 父命令。两项均为 P1：应把 signer 镜像化、自动发现模块/索引，并把“真实签名请求成功”纳入健康检查。

## 8. 生产验收状态

最终应用镜像为 `sha256:5da879fca7dab2f425e29c9b91468875b04cf9e157f4f848e6d54b2a73015295`（commit `613a305`，CI run `29901662897`）。生产快照确认 `slots=10 / queue=100 / Expand=2 / SSE target=1,max=6 / Download=8`。

fresh matched pair 消除了 403，但最终两次单请求分别在 27.04s 和 34.97s 返回真实 `usage_limit_reached` 429。根据 Panda 分阶门禁，单请求未通过便停止，没有运行 2/4/10。因此：

- 10 请求流水线、FIFO、同账号单飞及上下行拆分已实现并部署。
- “生产 10/10 同时生图成功”尚未验收，当前阻塞是 20 个 dispatch 账号的 Imagine 有效额度/soft-stop 接受率。
- 在补充并验证至少 10 个可生图账号、或现有模型额度恢复前，不得把 10 槽容量写成 10/10 成功能力。
