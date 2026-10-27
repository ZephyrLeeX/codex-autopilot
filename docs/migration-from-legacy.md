# 从私人脚本迁移(migration-from-legacy.md)

| 项 | 值 |
| --- | --- |
| 状态 | Phase 1 定稿 v1.0 |
| 覆盖需求 | 任务书 §五(文档 11:原有私人脚本到通用项目的迁移办法)、L4 验收 |
| 关联 | [deployment.md](deployment.md)、[testing.md](testing.md) §6 |

---

## 1. 旧系统画像(任务书给定的已知事实,仅作兼容设计输入)

- Debian 13,Codex CLI 0.164.0,tmux 托管 TUI,systemd timer 驱动;
- 额度状态来自 JSONL 快照;自动续作用 `codex queue`;
- 曾验证过一次真实额度自动恢复成功。
- 开发机**不存在**该环境:本迁移方案是剧本,不是已执行的记录;所有步骤在 L4 由用户在目标机执行(任务书 §六)。

## 2. 差异总览(旧 → 新)

| 维度 | 旧私人脚本 | codex-autopilot |
| --- | --- | --- |
| 会话目标 | 硬编码 | 注册表(SQLite,多会话) |
| 额度数据 | JSONL 快照单一来源 | App Server 实时优先 + JSONL 降级,来源标注 |
| 恢复判定 | (未知细节) | 三证据 + G1..G12 守门 + marker 确认 |
| 幂等 | (未知细节) | attempting 落库 + 崩溃清扫 + 唯一约束 |
| 通知/状态 | 无/简陋 | 事件通知 + audit + history |
| 部署 | systemd timer | 常驻服务(或 timer 模式) |

风险点:旧脚本行为未知细节(尤其重试/去重语义)意味着并行期可能出现**双重续作**(旧脚本与我们同时投递)。对策:并行期本工具一律 `auto_resume=off`(§4 步骤 3),并行期结束、退役旧 timer 后才开启。

## 3. 迁移前置检查(L2,目标机)

```bash
codex-autopilot doctor          # 全绿(尤其:queue --help、app-server 握手、sessions 可读)
codex-autopilot list --codex-sessions   # 找到旧脚本监控的会话 UUID
```

对照旧脚本配置中的 Session UUID / 项目路径 / tmux 会话名,整理迁移清单(每会话一行)。

## 4. 迁移剧本(用户执行)

1. **冻结旧脚本变更**:旧 timer 保持运行但不再改其配置;
2. **安装**(deployment.md §1/§3),先不注册任何会话;
3. **注册为纯观察模式**(逐会话):
   ```bash
   codex-autopilot add --session <UUID> --name backend --project /path --tmux-session <旧名>
   # 注意:不传 --auto-resume(默认关)
   systemctl --user enable --now codex-autopilot.service
   ```
4. **并行观察期(≥7 天)**:每天对比——
   - 额度耗尽事件:两者是否同时检出(autopilot `history`/通知 vs 旧脚本日志);
   - 误报/漏报:autopilot 检出的失败是否真实(抽查 rollout);
   - `codex-autopilot status` 无 NEEDS_HUMAN 之外的异常态;
5. **退役旧 timer**:`systemctl --user disable --now <旧 timer>`;确认旧脚本不再投递;
6. **逐会话开启自动恢复**(一次一个,先低风险会话):
   ```bash
   codex-autopilot resume --session backend --enable-auto-resume
   ```
   首次真实恢复后留存证据:`history --json`、audit 行、通知邮件(归档至 docs/evidence/,L4 证据);
7. **收尾**:旧脚本归档;`doctor` 终检。

## 5. 回滚

任意阶段可回滚:关闭会话 `auto_resume`(或全局 `pause --global`)→ 停服务 → 重启旧 timer。autopilot 的数据库保留(不冲突);彻底卸载见 deployment.md §6。

## 6. L4 验收判据

- 并行期:autopilot 检出全部旧脚本检出的额度耗尽事件(零漏报),且零自动投递;
- 开启后:至少一次真实"额度耗尽→自动恢复→marker 确认"全链路,证据齐全;
- 连续 14 天无重复投递、无错投、无需人工介入的非预期 NEEDS_HUMAN。

## 7. 旧数据迁移

旧脚本无可迁移结构化状态(仅日志);如需保留历史,人工把关键事件摘要录入 `docs/evidence/` 备查,不做自动导入(避免污染新审计链)。
