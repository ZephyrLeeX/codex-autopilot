# Linux 部署设计(deployment.md)

| 项 | 值 |
| --- | --- |
| 状态 | Phase 1 定稿 v1.0 |
| 覆盖需求 | FR-12 |
| 关联 | [cli-design.md](cli-design.md)、[persistence.md](persistence.md) §9 |

---

## 1. 安装

```bash
# 推荐:pipx(隔离环境,全局命令)
pipx install codex-autopilot            # Phase 7 发布 PyPI 后
pipx install git+https://github.com/<org>/codex-autopilot   # 开发期

# 或 pip(用户级)
python3 -m pip install --user codex-autopilot
```

要求:Python ≥3.11;无编译依赖(纯 wheel,NFR-08)。Arch 用户亦可打包 AUR(社区)。

## 2. 目录布局(XDG,全部可覆盖,零硬编码)

| 用途 | 默认路径 | 覆盖方式 |
| --- | --- | --- |
| 配置 | `~/.config/codex-autopilot/config.toml` | `--config` / `$XDG_CONFIG_HOME` |
| 数据库/锁/备份 | `~/.local/state/codex-autopilot/` | `--state-dir` / `$XDG_STATE_HOME` |
| 日志 | journald(服务)/ stderr(前台) | — |

多 OS 用户:各自家目录天然隔离,无需共享(需求 §2.2)。

## 3. systemd 用户服务(默认方案,非 root)

`systemd/codex-autopilot.service`(随包提供,内容见仓库):

```ini
[Unit]
Description=codex-autopilot session monitor
# 网络就绪后再启动,避免启动期额度查询全部失败
After=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/codex-autopilot run
Restart=on-failure
RestartSec=15
# 崩溃重启由 systemd 兜底;应用层启动清扫(persistence §7)保证状态一致
TimeoutStopSec=30

[Install]
WantedBy=default.target
```

安装步骤:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/codex-autopilot.service ~/.config/systemd/user/
systemctl --user daemon-reload
loginctl enable-linger "$USER"        # 注销后继续运行(无人值守的关键)
systemctl --user enable --now codex-autopilot.service
journalctl --user -u codex-autopilot -f   # 观察
```

注意:`ExecStart` 的二进制路径随安装方式不同(pipx 默认 `~/.local/bin`);提供 `codex-autopilot doctor --print-systemd-unit` 生成适配当前安装路径的单元文件,避免手工改路径。

## 4. 可选:timer 单次执行模式

偏好 cron 风格(无常驻)的用户:

```ini
# codex-autopilot-check.service(oneshot)
[Service]
Type=oneshot
ExecStart=%h/.local/bin/codex-autopilot tick

# codex-autopilot-check.timer
[Timer]
OnBootSec=2min
OnUnitActiveSec=90s
```

取舍:timer 模式下提前恢复的响应粒度 = timer 周期(90s 级);常驻模式的密集刷新可到 60s 且通知即时。两种模式都受单实例锁保护,可并存切换(但不要同时 enable 两个)。

## 5. 可选:系统服务(文档化,不默认)

需要为"无登录会话的服务账户"运行时:以该账户安装(家目录内),系统单元加 `User=<thatuser>`;**不要**用 root 运行(工具操作的是该用户的 `~/.codex`)。最小权限要点:无需任何特权(无 CAP、无 root 写路径);PrivateTmp=yes 可用; ProtectHome 不可用(需要访问家目录)。

## 6. 升级与卸载

```bash
pipx upgrade codex-autopilot && systemctl --user restart codex-autopilot
# 数据库迁移在启动时自动执行(persistence §6);升级说明附迁移内容
pipx uninstall codex-autopilot
systemctl --user disable --now codex-autopilot.service
rm -rf ~/.config/codex-autopilot ~/.local/state/codex-autopilot   # 可选:清数据
```

降级不支持(迁移单向);回滚 = 恢复升级前备份(§7)。

## 7. 备份与恢复

- 自动:每日 `VACUUM INTO` 快照至 state 目录,保留 7 份(persistence §9);
- 手动:`codex-autopilot doctor --backup`;
- 恢复:停服务 → 用备份替换 `state.db`(删 `-wal`/`-shm`)→ 启动(自动完整性检查+清扫);
- 迁机:备份 `config.toml` + 最新 db 快照 → 新机安装 → 恢复 → `doctor` 确认(注意:新机需能访问同一 `~/.codex` 的会话文件,否则会话状态 MISSING)。

## 8. 跨发行版要点(Debian / Ubuntu / Arch)

- 无发行版专属路径;Python 3.11+ 在三者当前稳定版均满足(Debian 13=3.13,Ubuntu 24.04=3.12,Arch=滚动);
- pipx 安装途径一致;CI 容器矩阵见 testing.md §7;
- 已知差异点仅:`loginctl enable-linger` 的 polkit 授权(个别发行版需管理员放行),doctor L0 会检测并提示。

## 9. 配置示例

见 [examples/config.toml](../examples/config.toml)(带注释全量示例);凭据一律环境变量/密码管理器命令,不落盘(NFR-09)。

## 10. 关键设计决策

| # | 决策 | 理由 | 被否决的替代 |
| --- | --- | --- | --- |
| D-DEP-1 | 用户服务+linger 为默认 | 非 root、免 sudo、随用户包管理走(任务书 FR-12) | cron(无监督/通知弱)、系统服务(权限过宽) |
| D-DEP-2 | doctor 生成适配路径的 unit 文件 | pipx/pip/包管理器路径各异,避免文档写死 | 文档手改(必有人改错) |
| D-DEP-3 | timer 模式保留但非默认 | 少数用户偏好;实现成本低(同一 tick) | 只留常驻(强加模型) |
