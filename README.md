# Codex Autopilot

> 通用 Codex CLI 会话监控与安全自动恢复系统(Linux)。
> 状态:**Phase 3 已完成(额度监控:rollout 解析/额度快照模型/App Server 只读客户端/`quota` 命令)**,Phase 4+ 开发中 —— 见 [docs/development-plan.md](docs/development-plan.md)。

Codex Autopilot 监控用户指定的 Codex CLI 会话,识别五小时/每周额度窗口耗尽导致的任务中断,并在满足全部安全条件后,用精确的会话 UUID 通过 `codex queue` 自动续作原任务。

**核心承诺:**

- 🗜️ **绝不**兑换重置券、付费、升级订阅或调用任何额度变更接口(代码级白名单 + 测试锁定)
- 🎯 **绝不**盲目投递:精确 UUID、幂等去重、人工消息检测、fail-closed
- 🖥️ **绝不**主动创建 Codex 进程;tmux 仅作运维信息,不作为决策依据
- 📊 额度数据标注来源与采集时间;历史快照绝不冒充实时额度

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/requirements.md](docs/requirements.md) | 需求规格(FR/NFR/场景清单) |
| [docs/phase0-research.md](docs/phase0-research.md) | Phase 0 技术调研(参考项目分析 + 官方接口核实) |
| [docs/architecture.md](docs/architecture.md) | 系统架构与关键技术决策 |
| [docs/session-management.md](docs/session-management.md) | 多会话/多账户数据模型与生命周期 |
| [docs/quota-monitoring.md](docs/quota-monitoring.md) | 双额度窗口监控设计 |
| [docs/recovery-state-machine.md](docs/recovery-state-machine.md) | 恢复状态机 |
| [docs/early-recovery.md](docs/early-recovery.md) | 提前恢复机制 |
| [docs/queue-safety.md](docs/queue-safety.md) | queue 投递安全与幂等 |
| [docs/persistence.md](docs/persistence.md) | SQLite 持久化与崩溃恢复 |
| [docs/notifications.md](docs/notifications.md) | 通知机制 |
| [docs/cli-design.md](docs/cli-design.md) | CLI 设计 |
| [docs/testing.md](docs/testing.md) | 测试计划与 L1–L4 验收分级 |
| [docs/deployment.md](docs/deployment.md) | Linux 部署(systemd) |
| [docs/migration-from-legacy.md](docs/migration-from-legacy.md) | 从私人脚本迁移 |
| [docs/development-plan.md](docs/development-plan.md) | 分阶段开发任务清单 |

## 快速开始(实现后可用)

```bash
pipx install codex-autopilot        # Phase 7 发布后
codex-autopilot add --session <UUID> --name backend --project /path/to/project
codex-autopilot doctor              # 环境自检(L0/L2 探测)
systemctl --user enable --now codex-autopilot.service
```

## 环境要求(第一版)

Linux · Python 3.11+ · systemd · 官方 Codex CLI ≥0.149(接口已在 0.162.1 上完成本地实证 + 上游源码核实,目标环境 0.164.0 即当前最新稳定版;详见 [docs/phase0-research.md](docs/phase0-research.md))

## 许可

MIT(占位,待最终确认)。本项目思路借鉴 [codex-auto-resume-windows](https://github.com/songyb111-gachon/codex-auto-resume-windows),未直接复制其代码;借鉴范围见 [docs/phase0-research.md](docs/phase0-research.md)。
