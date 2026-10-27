# 需求规格说明书(requirements.md)

| 项 | 值 |
| --- | --- |
| 状态 | Phase 1 定稿 v1.0(随开发滚动修订) |
| 读者 | 本项目开发者、评审者、未来贡献者 |
| 上游 | 项目任务书(2026-10-27) |
| 下游 | architecture.md 及全部专项设计文档、testing.md 测试矩阵 |

本文档是唯一的需求权威来源(source of truth)。所有设计文档与测试必须能追溯到本文的功能需求(FR)、非功能需求(NFR)与场景(S)编号。任务书原文中的原则性表述在此被转换为可验证的验收标准;与任务书冲突时以任务书为准并在此显式记录。

---

## 1. 项目目标与定位

Codex Autopilot 是一个**独立、可安装、可配置、可测试**的开源命令行工具 + 常驻服务,运行于 Linux,面向使用官方 Codex CLI 的用户:

1. 用户注册需要长期无人值守运行的 Codex 会话(指定会话 UUID);
2. 工具持续监控这些会话的执行状态与所在账户的五小时/每周额度窗口;
3. 当检测到任务因额度耗尽而中断时,记录失败轮次并制定恢复计划;
4. 在**满足全部安全条件**后(额度实际恢复、无人工介入、无重复投递风险),以精确 UUID 通过 `codex queue` 自动续作原任务;
5. 支持官方提前重置与用户手动兑换重置券后的提前恢复;
6. 全程只读查询额度,绝不执行任何付费、兑换、订阅变更类操作。

**定位边界**:本工具是"额度中断的守望者与安全续作者",不是任务编排器、不是 Codex 的替代前端、不做任何额度变更。

## 2. 支持范围

### 2.1 第一版支持

- Linux(优先验证 Debian、Ubuntu、Arch Linux;不硬编码任何发行版差异)
- Python 3.11+(仅依赖标准库 + click)
- systemd(用户服务为主,系统服务为可选方案)
- 官方 Codex CLI(接口事实以 [phase0-research.md](phase0-research.md) 的核实结果为准;开发参照 0.162.1,目标兼容 0.164.0+)
- tmux **托管**的 Codex TUI(tmux 仅作为运维信息源,见 FR-08/D14)
- 多用户各自的独立安装(每 OS 用户一套实例,不跨 OS 用户共享)

### 2.2 明确不做(Out of Scope,第一版)

| 项 | 处理 |
| --- | --- |
| Windows / macOS | 不支持;架构上通过端口层(Ports)隔离平台耦合,见 architecture.md |
| 非交互模式(`codex exec`)的监控与恢复 | 预留扩展点(FR 注册模型不排除),第一版不实现 |
| 兑换重置券 / 购买额度 / 任何付费操作 | **永久禁止**,见 NFR-01,不是"暂不支持" |
| 主动创建/启动 Codex 进程或 TUI | 永久禁止;只向已存在的会话投递消息 |
| 多个 OS 用户共享一个实例 | 不支持;每用户独立安装与数据目录 |
| 修改 Codex 会话历史 | 只读解析 JSONL,绝不写入 |

### 2.3 硬编码禁令(NFR-03)

以下内容不得出现在代码中:发行版路径、用户名、时区、真实 Session UUID、项目目录、代理地址、邮箱服务商(如 QQ)、具体 Codex CLI 版本号。配置示例只能使用占位值。测试证据:静态扫描测试(见 testing.md T-GUARD-04)。

## 3. 术语表

| 术语 | 定义 |
| --- | --- |
| 账户(Account) | 一个 `CODEX_HOME` 对应的 Codex 身份与额度归属单位。同一 Codex Home 下的所有会话**共享**账户额度。 |
| 会话(Session)/ 线程(Thread) | 一个 Codex 会话,由 UUID 唯一标识,对应 rollout JSONL 文件。 |
| 注册会话(Monitored Session) | 用户通过 `add` 命令纳入监控的会话,含名称、项目路径等元数据。 |
| 轮次(Turn) | 会话中的一轮任务执行(用户消息 → 模型执行 → 完成/中断)。 |
| 失败轮次(Failure / Failed Turn) | 检测到的、因额度耗尽(或其他可恢复原因)而中断的轮次记录。 |
| 额度窗口(Quota Window / Bucket) | 一个具有独立重置时间的额度桶(五小时窗口、每周窗口、未来可能的新窗口类型)。 |
| 额度快照(Quota Snapshot) | 某一时刻账户各额度窗口状态的记录,来源为 App Server(实时)或 JSONL(历史)。 |
| 恢复计划(Recovery Plan) | 针对某失败轮次的恢复意图:等待哪个窗口重置、何时重新评估。 |
| 投递(Delivery) | 一次 `codex queue --thread <UUID> --message <续作消息>` 调用及其回执。 |
| 确认(Confirmation) | 通过 JSONL/事件观察到续作消息已产生新轮次的事实。 |
| 人工介入(Human Intervention) | 用户在失败后亲自向会话发送消息、手动续作或取消任务。 |
| 守门(Guard) | 投递前必须全部通过的原子安全检查项,见 queue-safety.md。 |
| tick | 调度器的一个评估周期。 |

## 4. 功能需求

每条需求给出:描述、验收标准(可测)、主要设计约束(指向对应文档)。

### FR-01 通用会话注册

用户通过 CLI 注册会话;注册信息至少包含:会话 UUID、自定义名称、项目路径、Codex Home、是否开启监控、是否允许自动恢复、可选 tmux 会话标识、恢复消息模板、通知策略、可配置检查间隔。

**验收标准**

- FR-01-A1 `add` 拒绝非法 UUID(非 canonical 8-4-4-4-12 格式)并给出明确错误。
- FR-01-A2 `add` 警告或拒绝(可配)不存在的项目路径;Codex Home 不存在时拒绝。
- FR-01-A3 名称全局唯一;同一 UUID 不允许注册两次(FR-02-A4)。
- FR-01-A4 所有默认值(检查间隔、模板、通知策略)来自配置文件,代码中无用户特定默认值。
- FR-01-A5 `remove` 为软删除(保留历史),`list` 不再显示;提供 `--purge` 物理删除(需确认)。

设计:[session-management.md](session-management.md)

### FR-02 多会话监控

**验收标准**

- FR-02-A1 注册会话数量无代码上限;50 会话 / 10 账户规模下单 tick 完成(见 NFR-07)。
- FR-02-A2 各会话状态机独立演进;单会话处理异常被隔离,不影响其他会话(异常捕获 + 审计记录)。
- FR-02-A3 失败轮次与投递历史按会话独立保存,外键完整。
- FR-02-A4 `UNIQUE(uuid)` 约束阻止同一 UUID 重复注册。
- FR-02-A5 同一账户(同 Codex Home)的所有会话共享一份额度缓存与查询结果;额度状态归属于账户而非会话。
- FR-02-A6 不同 Codex Home 的额度快照在数据库中按账户隔离,任何查询/展示不得跨账户混合。
- FR-02-A7 每 tick 每账户至多发起一次实时额度查询(不论该账户下有多少会话)。

设计:[session-management.md](session-management.md)、[quota-monitoring.md](quota-monitoring.md)

### FR-03 五小时与每周额度识别

**验收标准**

- FR-03-A1 能解析出每个额度窗口的:使用百分比、窗口时长、重置时间(`resets_at`)、窗口身份标识。
- FR-03-A2 窗口类型判定优先依据结构化字段(如 `window_minutes`/`windowDurationMins`:300=五小时、10080=每周),**仅当结构化字段缺失时**才允许按 primary/secondary 位置推断,且推断结果必须标记 `inferred=true`。
- FR-03-A3 使用百分比 ≥ 阈值(默认 99%,可配 95–100)即视为该窗口"耗尽",触发额度失败判定;100% 与 99% 同样触发。
- FR-03-A4 支持双窗口同时处于耗尽状态:恢复必须等待**全部**阻塞窗口解除。
- FR-03-A5 支持额度桶超过两个(未来窗口类型):未知窗口类型记录为 `unknown`,按最保守策略处理(视为阻塞,直到确认为止)。
- FR-03-A6 快照缺失或过期时,状态为 `unknown`,**不得**推断为已恢复或仍耗尽;`unknown` 阻断自动恢复。
- FR-03-A7 识别 credits 类限制(`credits.hasCredits=false` 等)与 spend control 限制,标记为额度失败的一种并按需人工介入。
- FR-03-A8 恢复时间以数据源给出的 `resets_at` 为准;禁止用"窗口时长 + 起始时间"自行推算并当作权威值(推算值只能作为缺字段时的降级参考并标记低置信度)。

设计:[quota-monitoring.md](quota-monitoring.md)

### FR-04 实时额度查询(App Server,只读)

**验收标准**

- FR-04-A1 通过官方 App Server 的 `account/rateLimits/read` 方法查询;客户端方法白名单**仅含只读方法**,白名单之外的方法在客户端代码层面不可调用,并由测试锁定(见 NFR-01)。
- FR-04-A2 每次输出额度信息必须标注:来源(`appserver` / `jsonl`)、采集时间、是否过期(stale)。
- FR-04-A3 App Server 不可用时可展示 JSONL 历史快照,但必须标记为非实时;实时性判定规则见 quota-monitoring.md。
- FR-04-A4 查询失败不改变任何持久化状态机位置(除记录快照失败审计)。
- FR-04-A5 每账户查询频率受配置限制(默认等待期:常规 300s,存在等待恢复计划时 60s)。

设计:[quota-monitoring.md](quota-monitoring.md)、[phase0-research.md](phase0-research.md) §2

### FR-05 自动恢复(额度到期恢复)

**验收标准**

- FR-05-A1 恢复触发条件(全部满足):原失败轮次仍为该会话最新失败且未被取代;失败后无人工消息;无该失败的新投递记录;会话文件存在且可读;全部阻塞窗口已解除(判定规则见 quota-monitoring.md §5);监控与自动恢复均开启;未全局暂停。
- FR-05-A2 投递命令固定为 `codex queue --thread <SESSION_UUID> --message=<续作消息>`(单元素等号形式,防消息首字符被解析为选项),argv 数组构造,不经过 shell;**禁止**使用 `--last` 或任何猜测性目标。
- FR-05-A3 投递结果分类:已接受(退出码 0 且回执正则匹配,提取 queue_id)/ **结果未知**(非零退出、无输出、超时、崩溃——spawn 后一切不确定皆终态,绝不自动重试)/ 未启动(spawn 失败,唯一可退避重试类,至多 5 次)。
- FR-05-A4 queue 接受 ≠ 恢复成功;必须另行确认新轮次开始(确认机制见 queue-safety.md §6)。
- FR-05-A5 每个失败轮次至多一次成功投递(数据库唯一约束保证);重启进程/机器不破坏该保证。
- FR-05-A6 双窗口耗尽时,仅在两个窗口都解除后投递;官方提前重置任一窗口只更新对应窗口状态。

设计:[recovery-state-machine.md](recovery-state-machine.md)、[queue-safety.md](queue-safety.md)

### FR-06 提前恢复

**验收标准**

- FR-06-A1 等待期间以可配间隔做只读额度刷新,检测:重置时间提前(`resets_at` 变更)、使用率骤降、新窗口开始、重置券被用户兑换(`rateLimitResetCredits` 状态变化)。
- FR-06-A2 检测到提前恢复后,走与正常恢复**完全相同**的守门流程,不降低任何安全条件。
- FR-06-A3 `codex-autopilot check` 执行一次只读刷新 + 安全评估;默认与调度器行为一致(安全则投递),提供 `--no-deliver` 纯只读模式;绝不无条件强制续作。
- FR-06-A4 正常 `resets_at` 只是调度参考(决定何时开始密集刷新),不是"最早可投递时刻"的权威;投递时机由守门结果决定。
- FR-06-A5 提前恢复成功时发出 `early_recovery_detected` 通知。

设计:[early-recovery.md](early-recovery.md)

### FR-07 绝对禁止额度变更操作(红线)

工具不得:兑换重置券、购买额度、执行支付、升级订阅、切换用户账户、尝试绕过额度限制、调用任何具有上述效果的接口。

**验收标准(代码级强制)**

- FR-07-A1 App Server 客户端的方法调用层为显式白名单(仅只读方法,如 `account/rateLimits/read`、`initialize`、thread 只读方法);白名单测试枚举全部已注册方法,任何新增方法必须显式评审。
- FR-07-A2 显式黑名单测试断言以下方法**从未被引用**(出现即测试失败):`account/rateLimitResetCredit/consume`、`account/sendAddCreditsNudgeEmail`、`account/login/*`、`account/logout`、`marketplace/*`、`plugin/share/checkout`、`config/*write*`、`fs/*`(写)、`turn/start`、`thread/start` 等全部变更类方法(完整清单见 queue-safety.md §8)。
- FR-07-A3 外部进程执行层(CommandRunner)对 `codex` 子命令同样采用白名单:唯一允许的**变更类**命令是 `queue --thread --message`;`--help`/`--version` 类只读探测白名单另行列出。
- FR-07-A4 通知与文案中不得出现引导工具代为兑换/付费的功能或措辞。
- FR-07-A5 配置文件中的任何字段都不能扩展可执行命令范围(配置只能提供参数值,不能提供命令名/子命令名)。

设计:[queue-safety.md](queue-safety.md) §7–§9

### FR-08 自动恢复安全(fail-closed)

**验收标准**

- FR-08-A1 防重复:同一失败轮次重复续作被数据库唯一约束 + 状态机双重阻止。
- FR-08-A2 防超时盲试:queue 超时后结果记为 `unknown`,进入 NEEDS_HUMAN,不自动重试。
- FR-08-A3 防覆盖人工:失败后检测到用户消息(人工介入)即标记 `superseded`,停止该失败的恢复并通知。
- FR-08-A4 防打扰运行中任务:投递前检测会话当前无进行中的轮次(通过 JSONL 末尾事件与 app-server 可用时的线程状态);检测不确定时 fail-closed。
- FR-08-A5 防错投:目标 UUID 来自注册记录并经格式校验,不从 `--last`/名称匹配推断。
- FR-08-A6 用户取消(手动中断任务)后不自动重新启动:检测到任务被中断且无额度原因时,标记 needs_human 而非自动续作。
- FR-08-A7 进程/系统重启后不重复投递:投递前先写 `attempting` 记录,崩溃恢复时 `attempting` 一律转 `unknown` → needs_human(见 persistence.md §7)。
- FR-08-A8 JSONL 解析容忍尾部撕裂行(写入中):忽略不完整末行并记录警告;不基于不完整文件做失败判定或恢复确认。
- FR-08-A9 任何不能确认安全的状态,最终都落入 NEEDS_HUMAN 并通知,绝不"默认继续"。

设计:[queue-safety.md](queue-safety.md)、[recovery-state-machine.md](recovery-state-machine.md)

### FR-09 状态持久化

**验收标准**

- FR-09-A1 使用 SQLite(WAL 模式);保存:注册会话、账户与额度窗口标识、会话状态、失败轮次、额度快照、恢复计划、投递尝试、投递回执、恢复确认、通知记录、故障(审计)记录。
- FR-09-A2 多会话并发(守护进程 + CLI 同时访问)下:写事务 `BEGIN IMMEDIATE` + 忙等待,无丢失更新;关键唯一约束见 persistence.md。
- FR-09-A3 崩溃恢复:启动时执行完整性检查 + 未决投递清扫;损坏的库要求人工恢复并提供备份指引(doctor 报告)。
- FR-09-A4 数据库结构变更走编号迁移,禁止隐式改表。
- FR-09-A5 所有删除为软删除或归档,审计记录不可变(append-only)。

设计:[persistence.md](persistence.md)

### FR-10 管理 CLI

命令集:`add`、`remove`、`list`、`status`、`quota`、`history`、`check`、`pause`、`resume`、`doctor`,另有守护进程入口 `run` 与单次执行 `tick`(部署可选)。

**验收标准**

- FR-10-A1 所有命令支持 `--json` 机器可读输出;JSON schema 在 cli-design.md 固化并测试。
- FR-10-A2 `resume` 语义 = 恢复自动监控配置(undo pause);**不是**"发送任务";需要人工确认解除 NEEDS_HUMAN 时使用 `resume --session <name> --ack-needs-human`,必须显式承认。
- FR-10-A3 退出码语义固定(cli-design.md §6),脚本可依赖。
- FR-10-A4 `pause` 支持全局与单会话;暂停状态持久化并在重启后保持。
- FR-10-A5 `doctor` 输出分级检查清单(通过/失败/跳过),覆盖 L0(本地环境)与 L2(本机 Codex 接口探测)。

设计:[cli-design.md](cli-design.md)

### FR-11 通知

**验收标准**

- FR-11-A1 可插拔 sink 接口;第一版实现:console/log 与 SMTP(通用实现,任何服务商,不硬编码)。
- FR-11-B1 事件集:五小时额度耗尽、每周额度耗尽、提前恢复、queue 成功接受、已确认重新执行、恢复失败、需要人工介入、会话任务完成;另含:被人工取代、会话缺失(设计新增)。
- FR-11-A2 去重:每个事件有确定性 dedup key + 冷却窗口,重复事件在冷却期内不重复发送;dedup 记录持久化。
- FR-11-A3 通知发送失败不影响主流程:重试(指数退避,上限可配)后转入死信记录并审计。
- FR-11-A4 每会话可配通知策略(哪些事件、哪些渠道);全局默认策略可配。

设计:[notifications.md](notifications.md)

### FR-12 部署

**验收标准**

- FR-12-A1 标准 Python 包安装(pipx 优先);提供 CLI 入口 `codex-autopilot`。
- FR-12-A2 systemd 用户服务方案(默认)+ 可选 timer 单次执行方案 + 可选系统服务方案(文档化但不默认)。
- FR-12-A3 配置示例齐全;升级与卸载步骤明确;数据备份/恢复方法明确。
- FR-12-A4 所有路径遵循 XDG 规范且可覆盖;部署不要求特定家目录(如 /home/codex)。
- FR-12-A5 非 root 运行;系统服务方案中文档化最小权限要点。

设计:[deployment.md](deployment.md)

## 5. 非功能需求

| 编号 | 需求 | 验证方式 |
| --- | --- | --- |
| NFR-01 | 红线安全:见 FR-07;白名单/黑名单由自动化测试锁定 | T-GUARD-* 测试组 |
| NFR-02 | fail-closed:所有不确定状态收敛到 NEEDS_HUMAN | 状态机全转移测试 T-SM-* |
| NFR-03 | 无硬编码用户/路径/版本(见 §2.3) | 静态扫描测试 T-GUARD-04 |
| NFR-04 | 时区正确:内部一律 UTC 存储,展示按本地时区;日期变更线/DST 不产生错误调度 | 时钟注入测试 T-CLK-* |
| NFR-05 | 可测试性:时钟、Codex CLI、App Server、通知渠道全部为可注入接口;核心逻辑不依赖真实网络/进程 | 测试架构本身 |
| NFR-06 | 可观测性:结构化日志 + append-only 审计表;每次投递可完整回溯(why/when/what/receipt) | 审计 schema 测试 |
| NFR-07 | 性能:50 注册会话 / 10 账户下,单 tick(不含投递子进程等待)< 5s(普通硬件);额度查询按账户合并 | 性能冒烟测试 T-PERF-01 |
| NFR-08 | 打包:无编译依赖;纯 Python wheel;pipx 可装 | CI 构建测试 |
| NFR-09 | 隐私:默认不读取 `auth.json`/凭据文件;测试 fixture 全部人工构造/脱敏;日志与通知不包含会话内容,只含元数据 | 代码评审 + T-GUARD-05 |
| NFR-10 | 文档同步:每个 Phase 的行为变更必须同步更新对应设计文档;测试结论必须附证据 | PR 检查清单 |

## 6. 场景清单(测试矩阵锚点)

以下场景编号被 testing.md 引用;S-01~S-21 直接来自任务书要求的本地自动化测试清单,S-22 起为设计补充。

| 编号 | 场景 | 类别 |
| --- | --- | --- |
| S-01 | 五小时额度 100% 耗尽 → 检测失败 → 窗口重置 → 恢复 | 额度 |
| S-02 | 五小时额度 99% 耗尽(阈值触发) | 额度 |
| S-03 | 每周额度 100% 耗尽 | 额度 |
| S-04 | 每周额度 99% 耗尽 | 额度 |
| S-05 | 双窗口同时耗尽 → 仅一窗口解除 → 不恢复 → 全部解除 → 恢复 | 额度 |
| S-06 | 官方提前重置( resets_at 提前 / 使用率骤降)→ 提前恢复 | 提前恢复 |
| S-07 | 用户手动兑换重置券后额度变化 → 提前恢复(且工具自身未调用 consume) | 提前恢复 |
| S-08 | App Server 查询失败 → 降级 JSONL,标记非实时 | 数据源 |
| S-09 | 多账户额度桶并存,互不混淆 | 多账户 |
| S-10 | 多会话同时处于不同状态,互不影响 | 多会话 |
| S-11 | queue 返回成功(接受) | 投递 |
| S-12 | queue 无响应(进程挂起) | 投递 |
| S-13 | queue 已发送但调用端超时(结果未知) | 投递 |
| S-14 | 任务被用户提前人工恢复(失败后有用户消息) | 人工介入 |
| S-15 | 会话不存在(rollout 文件缺失/UUID 无效) | 异常 |
| S-16 | SQLite 崩溃恢复(kill -9 后重启) | 崩溃 |
| S-17 | 重复调度器实例(第二个实例被锁拒绝) | 并发 |
| S-18 | 网络异常(App Server 连接失败/中断) | 异常 |
| S-19 | 通知发送失败(SMTP 不可达) | 通知 |
| S-20 | 额度未知(快照缺失/解析失败) | 数据源 |
| S-21 | 不可恢复错误(如数据库损坏)→ 有诊断、不崩溃循环 | 异常 |
| S-22 | 失败后用户消息 → superseded + 通知,不投递 | 人工介入 |
| S-23 | JSONL 尾部撕裂行 → 忽略 + 警告,不误判 | 解析 |
| S-24 | 会话/全局暂停期间额度恢复 → 不投递,恢复监控后才评估 | 暂停 |
| S-25 | 未知额度窗口类型出现 → 按保守策略阻塞 | 兼容性 |
| S-26 | 任务正常完成 → task_complete 检测 + 通知,不投递 | 生命周期 |
| S-27 | 投递后确认超时(未见新轮次)→ NEEDS_HUMAN,不重投 | 投递 |
| S-28 | 运行中轮次存在 → 不投递 | 安全 |

## 7. 验收分级(L1–L4)

任务书要求严格区分本地模拟与真实环境,本项目的所有"通过"声明必须注明级别:

| 级别 | 定义 | 执行环境 | 证据 |
| --- | --- | --- | --- |
| L1 | 离线自动化测试(单元 + 模拟集成)全部通过 | 开发机/CI,无真实 Codex | CI 日志 + 测试报告 |
| L2 | 本机 Codex CLI 接口探测通过(`doctor`:二进制、版本、`queue --help`、App Server 握手 + 只读额度查询、sessions 目录可读) | 任意 Linux + 真实 Codex CLI,不投递任何消息 | doctor 输出 |
| L3 | 隔离测试会话实际验证:真实额度查询、真实 `codex queue` 投递到**专用测试会话**、确认新轮次 | 用户授权的机器 | doctor + history + 会话 JSONL 证据 |
| L4 | 生产环境迁移并验证:真实额度耗尽→自动恢复全链路 | 目标 Debian 服务器,用户授权 | 运行记录 + 审计 |

**声明规则**:任何文档/报告/提交信息声称"已验证"时必须注明级别;L1/L2 结果不得表述为 L3/L4。真实额度耗尽、官方提前重置等不易复现场景允许以 L1 覆盖为主、L3/L4 证据后补。

## 8. 需求追溯矩阵

| 需求 | 主设计文档 | 主要测试场景 |
| --- | --- | --- |
| FR-01 | session-management.md §2–§4 | T-CLI-ADD-* |
| FR-02 | session-management.md、architecture.md §3 | S-09、S-10、S-17 |
| FR-03 | quota-monitoring.md §3–§5 | S-01~S-05、S-20、S-25 |
| FR-04 | quota-monitoring.md §4、phase0-research.md | S-08、S-18 |
| FR-05 | recovery-state-machine.md、queue-safety.md | S-01~S-05、S-11 |
| FR-06 | early-recovery.md | S-06、S-07 |
| FR-07 | queue-safety.md §7–§9 | T-GUARD-* |
| FR-08 | queue-safety.md、recovery-state-machine.md | S-12~S-15、S-22~S-28 |
| FR-09 | persistence.md | S-16、S-17 |
| FR-10 | cli-design.md | T-CLI-* |
| FR-11 | notifications.md | S-19 |
| FR-12 | deployment.md | L2~L4 清单 |
| NFR-01/02 | architecture.md §5、queue-safety.md | T-GUARD-*、T-SM-* |

## 9. 与任务书的差异记录

- 任务书 FR-11 事件集基础上,设计新增 `superseded_by_human` 与 `session_missing` 两个事件(安全语义需要,见 notifications.md §2)。
- 任务书 FR-10 命令集基础上,新增 `run`(守护进程)与 `tick`(单次评估)两个内部命令(部署两种模式需要,见 deployment.md §4)。
- 其余无冲突;任务书中"示例命令 `codex-autopilot add ...`"的参数拼写以 cli-design.md 为准。
