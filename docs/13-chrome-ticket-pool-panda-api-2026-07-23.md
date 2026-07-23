# Chrome 票池 + grok2api 生图（2026-07-23）

## 状态

| 组件 | 状态 | 说明 |
|------|------|------|
| 本机 Chrome 开票（Python 原型） | ✅ E2E 已验收 | `tools/chrome_ticket_pool_minter.py` |
| Go 票池（`chrome_tickets` 表） | ✅ 已并入 grok2api | `backend/internal/application/chrometicket` |
| Admin API 入池/统计/清扫 | ✅ | `POST/GET /api/admin/v1/chrome-tickets*` |
| 生图链路取票 | ✅ | `image.go` → `attachChromeTicket` → `statsig` meta 覆盖 |
| 实验 `panda_ticket_image_api.py` | ⚠️ 可废弃 | 统一走 grok2api `/v1/images/generations` |

**部署门禁（2026-07-23）**：`go build ./...` + `go test ./...` 全绿；schema 自动迁移 `chrome_tickets`；Panda 部署后需跑「灌池 → stats → 生图」三步验收。

---

## FAQ

### 1. 生图 IP 出口必须和开票 IP 一致吗？

**不必一致，当前设计就是刻意分离的。**

| 阶段 | 出口 | 存什么 |
|------|------|--------|
| 本机 Chrome 开票 | 本机代理（如 `127.0.0.1:7897`） | 只捕获 **`statsig_meta` + 设备 Cookie**（`grok_device_id`、`x-userid`） |
| Panda 生图 | **udeal egress warm**（如 `70.39.164.200:30000`） | 使用账号 SSO + egress 的 **`cf_clearance`**，**不把本机 CF 写入票** |

票里**不含 IP、不含 `cf_clearance`**。开票与消费可以在不同 IP 上完成；Panda 侧由 `egress.Acquire` 为每个账号 warm 出口 Cookie。

注意：

- **账号绑定**：票按 `account_id` 入池/出池，须与 grok2api 路由到的 SSO 账号一致。
- **设备指纹**：票携带的 `grok_device_id` 来自本机 Chrome；与 Panda 出口 IP 组合是否触发风控，需线上观察。已验收路径（1467）在本机 IP 开票 + Panda udeal 生图成功。
- **asset 下载**：走独立 `ScopeAsset` 出口，与开票 IP 无关；若 asset 403，见 [12-web-lite-two-stage-failure-asset-403](./12-web-lite-two-stage-failure-asset-403-2026-07-23.md)。

### 2. 一张票能反复用吗？

**不能。当前实现为「pop 即消费」，每张票只服务一次 Lite SS 请求。**

```text
入池 (available) → PopForAccount → 立即 status=consumed → 用于一次 generateLiteImageURL
```

| 资产 | 能否复用 | 说明 |
|------|----------|------|
| 池内一条记录 | ❌ 单次 | `PopForAccount` 事务内标记 `consumed` |
| `statsig_meta` 内容本身 | 理论上可多次签名 | 6～24h 有效；但池层不复用，需重新入池 |
| 短效 `statsig` | ❌ 不入池 | ~45s；由 `grok-signer` 每次现签 |

同一 API 请求内多张图（fast/diverse）仍共享**同一次** `openChat` 上下文；每进入 `generateLiteImageURL` 循环会再 pop 一张新票（若池有货）。

无票时：**回退**原有「egress 抓首页 meta」路径，不阻断生图。

### 3. 10 并发要不要 10 个 Chrome？

**不需要。** 见下文「并发模型」。

---

## 架构

```text
本机 Chrome（低频批处理，单浏览器顺序 ~90s/账号）
    │  POST /api/admin/v1/chrome-tickets
    ▼
grok2api SQLite chrome_tickets（statsig_meta + device_cookie, TTL 12h）
    │
API N 并发（请求时 0 个 Chrome）
    │  pop 票（按 account_id）
    │  grok-signer POST /sign → 新鲜 x-statsig-id
    │  egress warm + Lite SSE + asset 下载
    ▼
回图（JPEG / grok2api media URL）
```

**不必在池里存 `statsig` 字符串**：Panda 已有 `grok-signer`（`172.22.0.2:8788`），用池内 `statsig_meta` 每次请求现签。Chrome 只负责周期性补充 meta 与设备指纹。

与 **纯 grok2api 生产链** 的关系：

- 生产已配 `statsigMode: url` + `grok-signer` sidecar（见 [signer-sidecar.md](./signer-sidecar.md)）。
- 票池是 **账号级 meta/设备证据** 缓冲层，已接入 `backend/internal/infra/provider/web/image.go`。
- 并发由 `imagepipeline` 的 `PromptSlots` / `SSESlots` 控制，与 Chrome 进程数解耦。

---

## 票 v2 格式

```json
{
  "version": 2,
  "account_id": 1467,
  "statsig": "",
  "statsig_meta": "4O4+MGluH3emyO0V78SQCC+...",
  "cookie": "grok_device_id=...; x-userid=...",
  "user_agent": "Chrome/150...",
  "sign_source": "ui_capture_appchat:..."
}
```

| 字段 | TTL | 说明 |
|------|-----|------|
| `statsig` | ~45s | **不入池**；Panda 用 meta + signer 现签 |
| `statsig_meta` | 6～24h（可配 `ttl_hours`） | `grok-site-verification`；**票池主资产** |
| `cookie` | 与 meta 同档 | 仅设备类；**不含** `cf_clearance`（Panda egress warm） |

---

## Go 模块（生产）

| 路径 | 作用 |
|------|------|
| `internal/domain/chrometicket/` | 领域类型 + context lease |
| `internal/repository/chrome_ticket.go` | 仓储接口 |
| `internal/infra/persistence/relational/chrome_ticket_*.go` | GORM + SQLite/Postgres |
| `internal/application/chrometicket/pool.go` | Push / Pop / Sweep / Stats |
| `internal/transport/http/chrometicket/handler.go` | Admin API |
| `internal/infra/provider/web/chrometicket.go` | 生图取票 + Cookie 合并 |
| `internal/infra/provider/web/statsig.go` | meta 覆盖，跳过首页抓取 |

---

## 工具链

| 工具 | 作用 |
|------|------|
| `tools/chrome_ticket_pool_minter.py` | **本机 Chrome 批量开票** → Go Admin API（首选）或 SSH 回退 |
| `tools/local_chrome_panda_lite.py` | 单次 E2E 调试（票即用） |
| `tools/panda_ticket_pool.py` | ⚠️ 旧版独立 SQLite；已被 grok2api 主库取代 |
| `tools/panda_lite_with_ticket.py` | 独立 worker 调试 |
| `tools/panda_ticket_image_api.py` | ⚠️ 实验 API，建议废弃 |
| `tools/_panda_export_sso.py` | 从 Panda DB 导出 SSO |
| `tools/_panda_find_imagine.py` | 查有 imagine 额度的账号 |

---

## 操作命令

### 导出 SSO（须有 imagine 额度）

```powershell
scp tools\_panda_export_sso.py panda:/tmp/
ssh panda "python3 /tmp/_panda_export_sso.py 1467" > .tmp\web-sso-canary-1467.json
```

### 批量入池（推荐：Go Admin API）

```powershell
$env:GROK2API_BASE = "https://your-panda-host"
$env:GROK2API_ADMIN_TOKEN = "your-admin-jwt"

python tools\chrome_ticket_pool_minter.py `
  --account-ids 1467,1470 `
  --sso-file .tmp\web-sso-canary-1467.json `
  --ttl-hours 12
```

未设置 `GROK2API_BASE` + `GROK2API_ADMIN_TOKEN` 时，minter 回退 SSH + `panda_ticket_pool.py`。

### Admin API（需 Bearer 管理员 JWT）

```bash
# 入池
curl -s -X POST "$BASE/api/admin/v1/chrome-tickets" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"account_id":1467,"statsig_meta":"...","cookie":"grok_device_id=...","ttl_hours":12}'

# 统计
curl -s "$BASE/api/admin/v1/chrome-tickets/stats" -H "Authorization: Bearer $TOKEN"

# 清扫过期
curl -s -X POST "$BASE/api/admin/v1/chrome-tickets/sweep" -H "Authorization: Bearer $TOKEN"
```

### 票池维护

- 后台任务：每 15 分钟自动 `sweep`（`application.go`）
- 池深告警：建议 `available` 按账号 < N 时告警（待接监控）

### 生图（走 grok2api 主网关）

```bash
curl -s "$BASE/v1/images/generations" \
  -H "Authorization: Bearer g2a_xxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-imagine-image","prompt":"a red apple"}'
```

日志关键字：`chrome_ticket_pool_hit`（取票成功）；Statsig 来源为 `chrome_ticket_pool` 时表示使用票内 meta 而非抓首页。

---

## 并发模型

| 场景 | Chrome 数量 | 说明 |
|------|-------------|------|
| N 并发生图 API | **0**（请求时） | pop 票池 + signer 现签；槽位由 `SSESlots` 限制 |
| 预先灌 M 张票 | **1**（顺序） | ~90s/张；M=10 ≈ 15min |
| 加快灌池 | 2～3（上限） | 多 Playwright context |
| 生产 grok2api | **0** | Chrome 仅周期性刷新 meta 池 |

要点：

1. 开票是**离线批处理**，与在线 QPS 解耦。
2. 并发读的是 **票池深度 + 流水线槽位 + udeal 出口**，不是 Chrome 数。
3. 账号 **imagine=0** 会 `systemErrCode:1010`；入池前用 `_panda_find_imagine.py` 筛号。

---

## 部署验收清单

### 构建（本机 / CI）

```powershell
cd backend
go build ./...
go test ./... -count=1
```

### Panda 部署后

| # | 步骤 | 期望 |
|---|------|------|
| 1 | 滚动发布 grok2api 二进制 | `/readyz` 200；DB 存在 `chrome_tickets` 表 |
| 2 | `GET /api/admin/v1/chrome-tickets/stats` | `by_status` 可为空 |
| 3 | minter 灌入 1467 至少 1 张票 | `available_by_account` 含 1467 |
| 4 | `POST /v1/images/generations`（imagine 有额度账号） | 200 + 图片；日志 `chrome_ticket_pool_hit` |
| 5 | 同账号连打直到池空 | 仍可能成功（回退首页 meta）；日志无 `pool_hit` |
| 6 | `POST .../chrome-tickets/sweep` | `expired` 数字合理 |

### 已知风险（部署后观察）

| 风险 | 缓解 |
|------|------|
| asset 403（udeal asset 出口） | 与票池无关；见 doc 12 |
| imagine 额度为 0 | 入池前筛号 |
| 池空 | 回退 signer 抓 meta；建议 cron minter + 池深告警 |
| 设备指纹 + 异地 IP 风控 | 观察 soft_stop 率；必要时同区域 egress |

---

## 验收记录

### Go 票池 Panda 部署（2026-07-23 18:30 CST）

| 项 | 结果 | 数据 |
|----|------|------|
| 二进制热更新 | ✅ | `/tmp/grok2api-chrometicket-pool-v2` → `docker cp` → `healthy` |
| DB 迁移 `chrome_tickets` | ✅ | 表 + 索引已创建 |
| Admin `POST /chrome-tickets` | ✅ | 票 `b4a7fca1…`，account **1467**，TTL 12h |
| Admin `GET /chrome-tickets/stats` | ✅ | `available: 1`（1467） |
| 本机 minter → Go API | ✅ | `pool_push_grok2api` 72s，`meta_len=64` |
| `/v1/images/generations`（公共 Key） | ❌ | 8 次均 **502**；日志 **asset 403**（539/559/289…），未路由到 1467 |
| Go `chrome_ticket_pool_hit` | ⏳ | 票未消费（公共 Key 未命中 1467） |
| Python E2E 1467（对照） | ✅ | Lite **200**，JPEG **130123B**，`asset_egress` 下载成功 |

**部署结论**：票池基础设施 **可上线**；主网关公共 Key 生图仍受 **账号路由 + asset 403** 影响，与票池无关。下一步：为测试 Key 绑定 1467，或修 udeal asset 出口后再验 `pool_hit`。

**运维备注**：首次 `docker cp` 误用了损坏二进制（SIGSEGV），已回滚；正确构建需 `-v /tmp:/out` 挂载宿主机输出。

### Python 原型 E2E（2026-07-23）

- 账号 **1467**：`http=200`，`url_count=4`，下载 **128660B JPEG**
- 账号 **1468**：`imagine remaining=0` → `systemErrCode:1010`

### Go 票池（2026-07-23）

- `go test ./internal/application/chrometicket/...` — Push/Pop/Sweep
- `go test ./internal/transport/http/chrometicket/...` — Admin handler
- `go test ./...` — 全量后端测试通过

**Panda 联调**：待本次二进制部署后执行上表步骤 2～4。

---

## 相关文档

- [http-reverse-lite-chain.md](./http-reverse-lite-chain.md) — Chrome 短签冻结链
- [12-web-lite-two-stage-failure-asset-403-2026-07-23.md](./12-web-lite-two-stage-failure-asset-403-2026-07-23.md) — 生产两阶段失败
- [signer-sidecar.md](./signer-sidecar.md) — grok-signer 部署
