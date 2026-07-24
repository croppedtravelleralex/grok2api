# Web 四池设计与生图成功率对照（2026-07-24）

> 回答：**纯 HTTP vs 开票生图各多少成功率？开票系统解决什么、不解决什么？**

## 1. 成功率对照（有证据的区间，非理论值）

统计时必须 **分层**，否则会把「票失效」「账号 429」「出口 CF」「号池 503」混成一句「成功率低」。

### 1.1 纯 HTTP（无 Chrome 票 / 无 `pool_hit`）

| 场景 | 样本 | HTTP 200（完整出图） | 主要失败 | 证据 |
|------|------|---------------------|----------|------|
| 旧生产镜像（signer 未对齐） | 80 次 | **0%** | 上游 403 | [08-image-pipeline-status](./08-image-pipeline-status-2026-07-22.md)、[plan.md](./plan.md) |
| signer 对齐后单请求 | 2 次 | **0%** | 真实 429（~27–35s） | [logs/2026/2026-07.md](./logs/2026/2026-07.md) 07-22 |
| udeal 零浏览器首日 e2e | 少量 | **有 200**（~7–11s） | 池薄时 429 | [07-udeal-zero-browser-ops](./07-udeal-zero-browser-ops-2026-07-21.md) |
| SSE 成功 → asset 下载 | 5 账号连续 bench | **0%**（阶段 2） | `assets.grok.com` **403** | [12](./12-web-lite-two-stage-failure-asset-403-2026-07-23.md) |
| Phase B smoke（BE-019 后，pin 4 号） | 10 次单并发 | **0%** | 6×429、4×502（soft_stop） | 2026-07-24，`21380ef5…0584` |

**纯 HTTP 结论**：

- **握手/SSE 层**：udeal 固定口 + signer 可把旧 80/80 403 打到「能连上、能开 SSE」。
- **完整 E2E**：无票时 **asset 下载 403 是高频杀手**；即便 SSE 成功也常死在阶段 2。
- **当前主瓶颈已不是 CF403（在 udeal 上）**，而是 **账号 Imagine 429 / soft_stop（502）** 与 **调度池准入过宽**（陈旧额度号进 dispatch）。

### 1.2 开票路径（`chrome_ticket_pool_hit`）

| 场景 | 样本 | HTTP 200 | 票命中但非 200 | 证据 |
|------|------|----------|----------------|------|
| S0 + 延迟 D-1/3/5m（1467） | 4 档 | **100%** | — | [14](./14-chrome-ticket-lifecycle-experiments-2026-07-23.md) |
| R-delay 全档（1574） | 8 probe | **100%** | — | [16](./16-chrome-ticket-experiments-round2-2026-07-23.md) |
| V-serial ×5 | 5 轮 | **80%**（4/5） | 1×502+pool_hit | 同上 |
| 票实验 jsonl 汇总 | 54 条 | **37%**（20/54） | 8×429、6×503、1×502（均 pool_hit 或混合） | [07](./07-udeal-zero-browser-ops-2026-07-21.md) §票实验 |
| **仅统计 pool_hit 子集** | 35 条 | **57%**（20/35） | 429/503/502 与票无关或弱相关 | 同上 |

**开票结论**：

- **票路径成立时**（`pool_hit=true`），在 **账号有额度、非 soft_stop** 的前提下，单并发 **200 率很高**（实验档多数 pass）。
- **整体 E2E 仍不高**，因为失败大量来自 **L3 账号限速（429）**、**L1 soft_stop（502）**、**号池空（503）**——这些 **不是票能修的**。
- **开票把「阶段 2 asset 403」从常见失败降为已修路径**（`c07cc2e`）；对比无票 5/5 下载失败，这是开票 **唯一被量化证明的增益**。

### 1.3 一句话对比

| 路径 | 解决什么 | 不解决什么 | 典型 E2E |
|------|----------|------------|----------|
| **纯 HTTP** | Panda 零浏览器、signer 现签、udeal 过 CF | asset 403、设备 meta 缺失 | 旧镜像 0%；无票 bench 阶段 2 常 0% |
| **开票 + HTTP** | meta/设备 cookie 缓冲、asset 下载链、本机 Chrome 与 Panda 解耦 | CF 出口质量、Imagine 429、soft_stop、号池调度 | pool_hit 且账号健康时 **≈80–100%**；混池实测 **≈37–57%** |

---

## 2. 开票系统的意义（职责边界）

开票 **不是**「提高整体成功率」的万能药；它是 **协议层证据供给**，把浏览器才能稳定拿到的 **`statsig_meta` + 设备 cookie** 搬到 Panda 纯 HTTP 链路上。

### 2.1 开票 **负责**

| # | 能力 | 为何纯 HTTP 不够 |
|---|------|------------------|
| 1 | **阶段 2 asset 下载** | 无票时 SSE 成功后 `assets.grok.com` 403 高发；有票 + warm + 下载头可 200 |
| 2 | **Panda 零浏览器** | 不在 2C/3.6G 机器上常驻 Chrome；开票在本机批处理 ~90s/张 |
| 3 | **跨 IP 消费** | 票不含 IP；本机开票 + Panda udeal 消费已验收（S0/D/R-delay） |
| 4 | **与 signer 分工** | 池存 **meta**（12h）；每请求 **现签** 短效 `x-statsig-id`（~45s） |
| 5 | **可观测** | `pool_hit` 把失败分层到 L0–L4（见 [15](./15-chrome-ticket-cf403-ip-reframe-2026-07-23.md)） |

### 2.2 开票 **不负责**

| # | 现象 | 正确归因 |
|---|------|----------|
| 1 | **CF403 / Webshare challenge** | **出口层**（L4）；须 udeal 等稳定住宅口，票救不了 |
| 2 | **429 usage_limit** | **账号 Imagine 额度**（L3）；换号，不是票过期 |
| 3 | **502 soft_stop** | **上游拒绝生图**（L1）；模型状态/冷却，不是票失效 |
| 4 | **503 无可用账号** | **号池调度**；pin∩dispatch 不一致（BE-019 已修索引，四池准入仍待做） |
| 5 | **「整体成功率稳定 95%+」** | 需要 **四池只放真实可用号** + 池深 + 多账号轮换，单靠开票做不到 |

### 2.3 若去掉开票会怎样

- Panda 回退「egress 抓首页 meta」→ **阶段 2 403 回归**（已实测）。
- 或在 Panda 上恢复 browser-bridge → **内存/CF 成本爆炸**（2GB+ 峰、不可扩展）。
- 纯 HTTP 只剩 signer + udeal，**链路可通但 E2E 不稳定**。

**因此开票的意义 = 在零浏览器前提下，补齐 Lite 两阶段链路的第 0 层证据（meta/cookie），并修通第 2 阶段下载；整体成功率由号池 + 出口 + 账号额度共同决定。**

---

## 3. Web Imagine 四池设计（对齐 Build 四池）

当前 Web 为 **三池**（`dispatch` / `recovery` / `dead`），`recovery` 与 `dispatch` 准入不对称，导致 **软停/耗尽号仍可进 dispatch**（尤其 pin 脚本洗 `available`）。

### 3.1 目标四池

| 池 | 常量（建议） | 准入 |
|----|-------------|------|
| **调度池** | `WebPoolDispatch` | `enabled` + `active` + pin 允许 + **`candidateImagineQuotaAdmissible`**（上游额度 fresh）+ `modelState ∈ {available, quota_available}` + 非 cooling |
| **普通池** | `WebPoolNormal` | `soft_stop` 冷却中、`quota_exhausted` 待恢复、额度陈旧待 Lite 探针、`imagineBlocked` |
| **验证池** | `WebPoolVerification` | 新入库、从未 Lite 探针、`modelState=unknown` |
| **删除池** | `WebPoolDelete` | `reauthRequired`、`signature_failed`、`deletable:`/`retired:` |

### 3.2 关键规则

1. **进 dispatch 标准 = Selector 的 `candidateImagineQuotaAdmissible`**，索引与 Acquire 同一函数。
2. **pin 脚本只写 route binding**，禁止洗 `status='available'` / 清 `cooldown_until`。
3. **dispatch 探针堆**（仿 Build `dispatchProbeHeap`）：调度池定期 Lite 探针，失败降级 normal。
4. **Chat 轨**暂保持三池或后续单独设计。

### 3.3 实现状态（BE-023 ✅ 2026-07-24）

- `WebPoolAt` Image 轨：`dispatch` / `normal` / `verification` / `delete`
- `imageDispatchAdmissible`：新鲜上游额度 + `modelState ∈ {available, quota_available}`
- Admin `GET /accounts/web-pools` 返回 `fourPools.image`
- `_panda_pin_imagine.py`：仅写 route binding，不再洗状态

---

## 4. Phase A/B 验收快照（2026-07-24）

| 项 | 结果 |
|----|------|
| 提交 | `96b664d` — BE-019/018/020/021 |
| 镜像 | `sha256:21380ef520018fb6d92166b003220ddd6a46385a904ad67173b2c13878110584` |
| pin 4 号 ∩ dispatch | ✅ `pinNotInDispatch=[]`，`dispatchImageLen=4` |
| 10× 探针 503 | ✅ 0 |
| 10× 生图 200 | ❌ 0/10（6×429，4×502） |
| egress 流量 API | ⚠️ smoke 路径 404，待修脚本 |
| 门禁 §4 | **部分通过**；生图 200 与四池准入待 BE-023 |

---

## 相关文档

- [plan.md](./plan.md) — 双池 + 门禁
- [15-chrome-ticket-cf403-ip-reframe-2026-07-23.md](./15-chrome-ticket-cf403-ip-reframe-2026-07-23.md) — 失败分层 L0–L4
- [11-web-three-pool-dual-probe-plan.md](./11-web-three-pool-dual-probe-plan.md) — 旧三池计划（将被四池取代）
