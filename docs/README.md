# grokImage 内部维护文档

本目录是 grokImage 的长期内部维护入口，服务对象是项目 Owner 和后续接手的 AI。
涉及当前状态、后续计划和维护判断时，优先以本目录为准。

## 阅读顺序

1. 先看 [01-project-charter.md](./01-project-charter.md) 理解项目目标和边界。
2. 再看 [02-current-state.md](./02-current-state.md) 确认当前真实状态。
3. **执行计划**看 [plan.md](./plan.md)（双池 + 流量 + 门禁）。
4. 需要判断优先级时看 [03-roadmap.md](./03-roadmap.md)。
4. 需要找长期改进入口时看 [04-improvement-backlog.md](./04-improvement-backlog.md)。
5. 需要继续接手维护时看 [05-ai-maintenance-playbook.md](./05-ai-maintenance-playbook.md)。
6. Build 号池状态机看 [08-build-four-pool-dual-probe-todos-2026-07-22.md](./08-build-four-pool-dual-probe-todos-2026-07-22.md)。
7. Imagine 额度、模型状态和 10 并发接续看 [09-imagine-quota-model-state-and-10-concurrency-todos-2026-07-22.md](./09-imagine-quota-model-state-and-10-concurrency-todos-2026-07-22.md)。
8. Web Lite 两阶段失败（soft_stop / asset 403）看 [12-web-lite-two-stage-failure-asset-403-2026-07-23.md](./12-web-lite-two-stage-failure-asset-403-2026-07-23.md)。
9. Chrome 票池 + Panda 生图 API 看 [13-chrome-ticket-pool-panda-api-2026-07-23.md](./13-chrome-ticket-pool-panda-api-2026-07-23.md)。
10. Chrome 票生命周期实验（分批短跑）看 [14-chrome-ticket-lifecycle-experiments-2026-07-23.md](./14-chrome-ticket-lifecycle-experiments-2026-07-23.md)。
11. CF403 / IP 认知重排看 [15-chrome-ticket-cf403-ip-reframe-2026-07-23.md](./15-chrome-ticket-cf403-ip-reframe-2026-07-23.md)。
12. 票实验第二轮 + Admin 密码三源看 [16-chrome-ticket-experiments-round2-2026-07-23.md](./16-chrome-ticket-experiments-round2-2026-07-23.md)。
13. **Web 四池 + 生图成功率 / 开票意义**看 [17-web-four-pool-and-imaging-success-rates-2026-07-24.md](./17-web-four-pool-and-imaging-success-rates-2026-07-24.md)。
14. 需要追溯历史上下文时看 [logs/](./logs/) 下的月度记录。

## 目录地图

| 文件 | 作用 | 何时更新 |
| --- | --- | --- |
| [01-project-charter.md](./01-project-charter.md) | 定义项目愿景、目标用户、完成态和边界 | 目标发生根本变化时 |
| [02-current-state.md](./02-current-state.md) | 当前状态主档 | 每次重要开发、修复、评审或计划调整后 |
| [plan.md](./plan.md) | **双池 + 流量统计实施计划**、门禁、**Python PoC → Rust/Go 分层**、多 subagent 分工 | 号池/票池/流量/实验恢复策略变更时 |
| [03-roadmap.md](./03-roadmap.md) | 阶段性路线图和里程碑 | 优先级或阶段目标变化时 |
| [04-improvement-backlog.md](./04-improvement-backlog.md) | 长期改进池 | 出现新问题、新想法或新风险时 |
| [05-ai-maintenance-playbook.md](./05-ai-maintenance-playbook.md) | AI 接手与回写规则 | 维护流程变化时 |
| [06-open-todos-2026-07-16.md](./06-open-todos-2026-07-16.md) | 2026-07-16 现场确认的开放待办（探活可视化 / CF403 / HTTP逆向） | 执行或关闭这些待办时 |
| [07-udeal-zero-browser-ops-2026-07-21.md](./07-udeal-zero-browser-ops-2026-07-21.md) | udeal 住宅 + 零浏览器签名器生产运维事实 | 出口/签名/调度变更时 |
| [08-build-four-pool-dual-probe-todos-2026-07-22.md](./08-build-four-pool-dual-probe-todos-2026-07-22.md) | Build 四池+双探针：已做/部分做/未做全量盘点与 FP-* 待办 | 四池方案推进或验收时 |
| [09-imagine-quota-model-state-and-10-concurrency-todos-2026-07-22.md](./09-imagine-quota-model-state-and-10-concurrency-todos-2026-07-22.md) | Imagine 次数、独立模型状态、10 并发：已做/未做/分档验收清单 | Web Lite 生图继续开发、部署或验收时 |
| [pure-http-transfer-and-zero-browser.md](./pure-http-transfer-and-zero-browser.md) | 票/钥/CF/零浏览器分层与可验证 AC | 纯 HTTP 结论变化时 |
| [http-reverse-lite-chain.md](./http-reverse-lite-chain.md) | **冻结** Chrome 短签 + curl_cffi HTTP Lite 开发链路 | 出票/Lite canary 行为变化时 |
| [12-web-lite-two-stage-failure-asset-403-2026-07-23.md](./12-web-lite-two-stage-failure-asset-403-2026-07-23.md) | Panda 生图两阶段失败、asset 403、探针与 Chrome/生产链路对照 | 排障或出口/重试策略变更时 |
| [13-chrome-ticket-pool-panda-api-2026-07-23.md](./13-chrome-ticket-pool-panda-api-2026-07-23.md) | Chrome 票池、Panda signer 现签、持续灌池、并发与 Chrome 关系 | 票池/开票/生图 API 开发时 |
| [14-chrome-ticket-lifecycle-experiments-2026-07-23.md](./14-chrome-ticket-lifecycle-experiments-2026-07-23.md) | 票存活/延迟/复用/换 IP/多账号并发；**分批 ≤10min** | 跑票池实验或定池深策略时 |
| [15-chrome-ticket-cf403-ip-reframe-2026-07-23.md](./15-chrome-ticket-cf403-ip-reframe-2026-07-23.md) | 有票路径下 CF403/IP 漂移结论重排；失败分层 L0–L4 | 排障或改写「必须同 IP」表述时 |
| [16-chrome-ticket-experiments-round2-2026-07-23.md](./16-chrome-ticket-experiments-round2-2026-07-23.md) | Admin 三源密码；第一/二轮票实验矩阵与停因 | 续跑 R-delay/S/V/mint 或改 admin 认证时 |
| [17-web-four-pool-and-imaging-success-rates-2026-07-24.md](./17-web-four-pool-and-imaging-success-rates-2026-07-24.md) | 纯 HTTP vs 开票成功率；开票职责边界；Web 四池设计 | 评估开票价值、四池实现或写门禁时 |
| [logs/](./logs/) | 月度历史记录 | 每轮工作结束时追加 |

## 真相来源优先级

1. 代码、配置、数据库、测试和命令结果
2. [02-current-state.md](./02-current-state.md)
3. [03-roadmap.md](./03-roadmap.md)
4. [logs/](./logs/)
5. 项目根目录 README

## 更新纪律

- 新事实先更新 [02-current-state.md](./02-current-state.md)
- 新路线或优先级变化更新 [03-roadmap.md](./03-roadmap.md)
- 新风险、技术债、改进想法更新 [04-improvement-backlog.md](./04-improvement-backlog.md)
- 每轮工作摘要追加到 `logs/YYYY/YYYY-MM.md`
- 历史日志只追加，不回写旧条目
