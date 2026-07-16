# grokImage 内部维护文档

本目录是 grokImage 的长期内部维护入口，服务对象是项目 Owner 和后续接手的 AI。
涉及当前状态、后续计划和维护判断时，优先以本目录为准。

## 阅读顺序

1. 先看 [01-project-charter.md](./01-project-charter.md) 理解项目目标和边界。
2. 再看 [02-current-state.md](./02-current-state.md) 确认当前真实状态。
3. 需要判断优先级时看 [03-roadmap.md](./03-roadmap.md)。
4. 需要找长期改进入口时看 [04-improvement-backlog.md](./04-improvement-backlog.md)。
5. 需要继续接手维护时看 [05-ai-maintenance-playbook.md](./05-ai-maintenance-playbook.md)。
6. 需要追溯历史上下文时看 [logs/](./logs/) 下的月度记录。

## 目录地图

| 文件 | 作用 | 何时更新 |
| --- | --- | --- |
| [01-project-charter.md](./01-project-charter.md) | 定义项目愿景、目标用户、完成态和边界 | 目标发生根本变化时 |
| [02-current-state.md](./02-current-state.md) | 当前状态主档 | 每次重要开发、修复、评审或计划调整后 |
| [03-roadmap.md](./03-roadmap.md) | 阶段性路线图和里程碑 | 优先级或阶段目标变化时 |
| [04-improvement-backlog.md](./04-improvement-backlog.md) | 长期改进池 | 出现新问题、新想法或新风险时 |
| [05-ai-maintenance-playbook.md](./05-ai-maintenance-playbook.md) | AI 接手与回写规则 | 维护流程变化时 |
| [06-open-todos-2026-07-16.md](./06-open-todos-2026-07-16.md) | 2026-07-16 现场确认的开放待办（探活可视化 / CF403 / HTTP逆向） | 执行或关闭这些待办时 |
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
