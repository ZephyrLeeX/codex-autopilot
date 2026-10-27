# CLI 设计(cli-design.md)

| 项 | 值 |
| --- | --- |
| 状态 | v1.2(Phase 3 实现:add/remove/list、quota、--json schema、env 命名、退出码已固化) |
| 覆盖需求 | FR-10、FR-06-A3、NFR-03 |
| 关联 | [architecture.md](architecture.md)、[deployment.md](deployment.md) |

---

## 1. 总则

- 框架:click(唯一运行时第三方依赖)。子命令即模块边界,`cli/` 层只做参数解析与输出格式化,业务逻辑全部在领域层(可测)。
- 全局选项(所有命令**之前**给出):`--config <path>`(指定配置文件)、`--state-dir <path>`(覆盖数据目录)、`--json`(机器可读输出)、`--verbose/--quiet`、`--no-color`。全局选项置于子命令之后视为用法错误(退出码 2)。
- 名字参数统一既可传 `name` 也可传完整 UUID(`resolve_session()` 二选一;**绝不**做前缀模糊匹配——错投风险,见 queue-safety.md G5)。
- 机器可读输出契约:`--json` 时 stdout 仅一行 JSON(或 JSONL,流式命令),人类可读信息走 stderr;退出码见 §6。失败时 stdout 为单行 `{"error": {"code", "message"}}`,stderr 同步人读信息。

## 2. 命令一览

```
codex-autopilot add        注册会话进监控
codex-autopilot remove     移除(软删除)
codex-autopilot list       列出注册会话(--codex-sessions 扫描可注册会话)
codex-autopilot status     会话/全局状态详情
codex-autopilot quota      额度查询(标注来源/时间/新鲜度)
codex-autopilot history    恢复与投递历史
codex-autopilot check      一次只读刷新 + 安全评估(默认可投递则投递;--no-deliver 纯只读)
codex-autopilot tick       单次调度周期(等价守护进程一个 tick;systemd timer 模式用)
codex-autopilot run        守护进程(常驻循环)
codex-autopilot pause      暂停(--session 指定单个,缺省全局)
codex-autopilot resume     恢复监控(--session 单个;--ack-needs-human 解除人工介入态)
codex-autopilot doctor     环境与接口自检(L0/L2)
```

## 3. 命令细节

### add
```
codex-autopilot add --session <UUID> --name <name> [--project <path>]
                    [--codex-home <dir>] [--tmux-session <name>]
                    [--resume-template <text>] [--check-interval <sec>]
                    [--auto-resume] [--notify-policy <json|@file>]
                    [--allow-missing-project] [--json]
```
校验规则见 session-management.md §2。默认 `--project` 取当前目录。`--auto-resume` 必须显式给出(默认关)。另有 `--description`(可选显示名,session-management §2.3)。

**`--json` 输出 schema(Phase 2 已固化,测试锁定)**:

```json
{
  "name": "str", "uuid": "str", "description": "str|null",
  "project_path": "str(absolute)", "codex_home": "str(realpath)",
  "account_label": "str|null", "enabled": true, "auto_resume": false,
  "tmux_session": "str|null", "check_interval": "int|null",
  "created_at": "int(unix sec)"
}
```

### remove / list / status
- `remove <name> [--purge --yes]`;`--json` 输出 `{"name", "uuid", "purged"}`。`--purge` 需 `--yes`(缺省退出码 2)。
- `list`:表格列 `NAME UUID ACCOUNT ENABLED AUTO_RESUME STATE LAST_EVENT`;`--json` 输出数组,元素 schema 固化:

```json
{
  "name": "str", "uuid": "str",
  "account": {"codex_home": "str", "label": "str|null"},
  "enabled": true, "auto_resume": false,
  "state": "str|null", "state_since": "int|null",
  "last_event_at": "int|null", "created_at": "int"
}
```

- `list --codex-sessions [--account <name>] [--days 30]`:扫描 Codex Home 展示可注册会话(只读)。**Phase 4(P4-T1)交付,依赖 rollout 解析器**。
- `status [name]`:缺省全部;单会话时展示状态机当前态、活跃失败与计划、最近投递与确认、tmux 探测(如有)。

### quota
```
codex-autopilot quota [--account <name>] [--refresh] [--source appserver|jsonl|auto] [--json]
```
- 默认展示最近快照(不强制刷新);`--refresh` 触发一次实时查询(App Server,只读)。账户选择:显式 `--account`(label 或 codex_home 精确匹配)> 有监控会话的账户 > 全部已知账户;空库时对配置默认 Codex Home 建立账户(quota 可独立于注册使用)。
- 实时查询失败:退出码仍 0(信息性),stderr 警告并降级展示库存数据,来源徽标如实标注历史/非实时(S-08);`--refresh` 与 `--source jsonl` 同给时警告后者忽略。
- 人读输出(英文,§4;示例为 Phase 3 实际输出):
```
account: .codex (/home/user/.codex)
  source: appserver (realtime)   captured: 2026-10-27 19:38:27 (+0800)   fresh
  five_hour  ██████████ 98% used (2% left)   resets: 2026-10-21 19:49:28 (+0800) [window_minutes=300]
  weekly     ████░░░░░░ 41% used (59% left)  resets: 2026-10-27 19:19:25 (+0800) [window_minutes=10080]
```
- `--json`(Phase 3 已固化,测试锁定)输出窗口行数组,每行 schema:

```json
{
  "account": {"codex_home": "str", "label": "str|null"},
  "source": "appserver|jsonl", "captured_at": "int(unix sec)", "stale": false,
  "window_kind": "five_hour|weekly|credits|spend_control|unknown",
  "inferred": false, "used_percent": 98.0, "resets_at": 1792583368,
  "window_minutes": 300, "exhausted": true
}
```
无数据的账户在人读模式给出提示行;`--json` 模式下无行(空数组)。

### history
```
codex-autopilot history [--session <name>] [--since <date>] [--limit N] [--json]
```
按时间倒序列出:失败记录(kind/检测时间)、恢复计划、投递(outcome/receipt 摘要)、确认(method)。`--json` 为结构化数组。

### check(FR-06)
```
codex-autopilot check [--session <name>] [--no-deliver] [--json]
```
单次:刷新额度(实时优先)→ 全会话安全评估 → 若有到期且全部守门通过的计划则执行投递。`--no-deliver` 保证零副作用(纯只读评估)。输出评估明细(每个等待中计划:各守门项通过/未通过及原因)——这是用户理解"为什么不恢复"的主要窗口。

### tick / run
- `tick`:等价守护进程一个周期(获取锁,失败即退出码 21);供 systemd timer 模式。
- `run [--foreground]`:常驻循环(内部时钟调度,见 architecture.md §4);信号处理(SIGTERM 优雅退出:完成当前 tick、清扫、落盘)。

### pause / resume
- `pause [--session <name>]`:无 `--session` = 全局暂停。
- `resume [--session <name>] [--ack-needs-human]`:恢复监控。会话处于 NEEDS_HUMAN 时必须显式 `--ack-needs-human` 才回到 ACTIVE(确认"我知道工具曾停手,人工已处理")。

### doctor
分级输出检查清单(PASS/FAIL/SKIP + 修复建议),`--json` 输出结构化结果。检查项:

| 级 | 检查 |
| --- | --- |
| L0 | Python ≥3.11;配置文件可解析;数据目录可写;数据库完整性与版本;单实例锁状态 |
| L0 | 通知渠道配置自检(SMTP 可达性可选探测 `--probe-smtp`,默认不发包) |
| L2 | `codex` 二进制存在、版本记录(与已知兼容范围比对提示);`codex queue --help` 退出码 0;共享 app-server daemon 探测(`codex app-server daemon version`,只读) |
| L2 | `codex app-server --help` 存在;App Server 握手 + `account/rateLimits/read` 只读查询成功(**不发送任何消息**) |
| L2 | 各注册会话 rollout 文件存在、可解析、UUID 一致;tmux 会话探测(如配置) |
| L2 | systemd 用户单元安装状态(如在 systemd 环境下) |

## 4. 输出与国际化

- 人读输出语言:英文(开源惯例,避免依赖 gettext 复杂度);错误码稳定可查文档。此为取舍:第一用户群中文,但开源工具英文输出 + 中文文档更通用。
- 时间展示:默认本地时区 + UTC 并注(`2026-10-27 15:02:11 (+08)`);`--utc` 切换。

## 5. 配置文件

位置:`$XDG_CONFIG_HOME/codex-autopilot/config.toml`(示例见 [examples/config.toml](../examples/config.toml);XDG 变量须为绝对路径,相对值拒绝)。段落:`[paths]`(state_dir 等)、`[scheduler]`(间隔)、`[quota]`(阈值/新鲜度 TTL/查询频率)、`[recovery]`(确认方式与超时、模板全局默认)、`[notify]`/`[notify.smtp]`、`[codex]`(二进制路径、默认 codex_home)。

优先级(高到低):CLI 参数 > 环境变量 > 配置文件 > 内置默认值。

**环境变量命名(Phase 2 固化)**:`CODEX_AUTOPILOT_<SECTION>__<KEY>`,段路径用双下划线连接,键名与 TOML 键一致(如 `CODEX_AUTOPILOT_PATHS__STATE_DIR`、`CODEX_AUTOPILOT_CODEX__CODEX_HOME`、`CODEX_AUTOPILOT_NOTIFY__SMTP__HOST`)。布尔取 `true/false/1/0/yes/no/on/off`;列表为逗号分隔。未知键在 TOML 中一律拒绝(防拼写错误静默弱化安全参数);未知环境变量忽略(如 `CODEX_AUTOPILOT_SMTP_PASSWORD` 是凭据变量而非配置键)。

**Phase 2 校验要点**:阈值区间(quota.exhausted_threshold_pct ∈ [95,100]、hint_min_pct ∈ [90,98]、resume_max_pct < exhausted_threshold_pct)、SMTP 启用时必填字段与 password_env/password_cmd 二选一、路径必须可解析为绝对路径(支持 `~`)。校验失败 → 退出码 20。

## 6. 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功(含"评估完成,无需动作") |
| 1 | 一般错误 |
| 2 | 用法错误(click 保留) |
| 10 | 存在 NEEDS_HUMAN 的会话(status/check/doctor 主动报告时) |
| 11 | 额度阻塞中,未投递(信息性,check 评估结果) |
| 12 | 存在结果未知的投递(需人工确认) |
| 20 | 配置/数据库错误 |
| 21 | 单实例锁被占(另一实例运行中) |
| 30 | doctor 有 FAIL 项 |

## 7. 关键设计决策

| # | 决策 | 理由 | 被否决的替代 |
| --- | --- | --- | --- |
| D-CLI-1 | click,唯一三方依赖 | 成熟、分组命令、补全;argparse 嵌套子命令冗长 | typer(多一层 pydantic 依赖)、rich(输出依赖,体积/复杂度) |
| D-CLI-2 | name 与完整 UUID 双键,无前缀匹配 | 防错投(与 G5 同源) | 前缀匹配(方便但危险) |
| D-CLI-3 | check 默认可投递(与调度一致),`--no-deliver` 只读 | check 的价值在"立即评估并行动";强制只读会让用户绕过工具手动续作(更不安全) | 默认只读(用户会手工 queue,失去守门保护) |
| D-CLI-4 | NEEDS_HUMAN 解除需显式 ack | 防止静默恢复掩盖未读告警 | 直接 resume(用户可能没看通知) |
