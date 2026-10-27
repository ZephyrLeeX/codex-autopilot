# Phase 0 技术调研报告(phase0-research.md)

| 项 | 值 |
| --- | --- |
| 状态 | Phase 0 完成 v1.0(开放问题见 §5;上游版本史待补充项已标注) |
| 覆盖需求 | 任务书 §四(参考项目)、§八(Phase 0)、FR-04(接口核实) |
| 证据 | 本地只读探测(docs/evidence/2026-10-27-phase0-local-probes.md,L2 级)+ 参考项目源码通读 + 官方 schema 导出 |

本报告区分三类事实:**[实证]**(本地二进制/文件直接验证)、**[参考]**(参考项目源码验证,版本环境可能不同)、**[待核]**(尚未验证,禁止当作已支持——任务书规则 16)。

---

## 1. 调研结论一览(给赶时间的读者)

1. `codex queue --thread <UUID> --message=<TEXT>` 在 0.162.1 **[实证]** 与 0.164.0(生产,任务书给定)均可用;回执为单行文本 `Queued message <uuid> for thread <uuid>.` **[参考]**。
2. App Server 只读额度方法名确认为 `account/rateLimits/read` **[实证 schema]**;协议为 stdio 上的 NDJSON JSON-RPC **[参考]**。
3. 额度快照有两套字段命名:App Server camelCase(`usedPercent/windowDurationMins/resetsAt`)、JSONL snake_case(`used_percent/window_minutes/resets_at`)**[双实证]**;窗口类型靠时长(300/10080 分钟)区分,v2 协议**无 window_type 字段**。
4. 协议中存在 `account/rateLimitResetCredit/consume`(重置券消费)与 `thread/queue/delete`(撤回队列消息)**[实证 schema/参考]** —— 前者永久黑名单,后者 v1 不启用。
5. Codex 数据存储:0.162.1 CLI **[实证]** 与 0.164 Windows 桌面 **[参考]** 均有代数后缀 SQLite 三库(`~/.codex/thread_history_1.sqlite` 表 thread_turns/thread_items、`queue_1.sqlite` 表 queued_items、`state_5.sqlite`)——结构化检测/确认/队列观察两代版本可用;JSONL rollout 为恒备回退(初测曾误判 CLI 无此库,已复核更正,见 evidence §4)。
6. 参考项目 MIT 许可,允许借鉴与复用;其投递安全协议(marker 幂等、spawn 后不确定即终态、claim 先落库)经评估后采纳为本项目设计的基础。
7. `codex queue` 的投递语义已源码级查清:消息持久化于 `~/.codex/queue_N.sqlite`,由共享 daemon 在目标线程**已加载且空闲**时派发(`turn_trigger=="queue"` 可精确区分队列派发与人工输入 `"user"`);`--no-daemon` 报错;0.164.0 无 `--resume`(≥0.165 引入,未来可评估作为无 TUI 投递的受审白名单扩展)。
8. 版本定位:生产目标 0.164.0 即当前最新稳定版(2026-10-23);queue 自 0.149.0 引入;额度协议近期无破坏性变更。

## 2. 官方接口核实明细

### 2.1 `codex queue` 子命令

| 事实 | 级别 | 依据 |
| --- | --- | --- |
| 存在;必选参数 `--thread <THREAD> --message <TEXT>` | [实证] | 0.162.1 `codex queue --help` + 上游源码 `codex-rs/cli/src/queue_cmd.rs` |
| `--thread` 接受 "Session UUID **or exact session name**" | [实证] | 同上;设计决策:只用 UUID 形式 |
| 成功回执(退出码 0)单行:`Queued message <queue-uuid> for thread <thread-uuid>.` | [参考] | 参考项目 transport.py 正则;源码确认返回形态 |
| **投递语义(源码确认)**:CLI 把消息经 app-server 方法 `thread/queue/add` 持久化到 `~/.codex/queue_N.sqlite.queued_items` 后立即返回;**不启动轮次**;daemon 内 watcher 每 10s 轮询,仅当目标线程**已加载且空闲**时派发为新轮次(`turn_attribution.turn_trigger == "queue"`) | [源码确认+实证库结构] | `codex-rs/ext/queue/src/service.rs`、`thread_queue_processor.rs`;本机 `queue_1.sqlite` 实证 |
| **依赖后台进程**:须能发现共享本地 app-server daemon(或 `--remote`);`--no-daemon` 直接报错 | [源码确认] | `queue_cmd.rs` |
| 线程未加载(TUI 未开、daemon 未 resume)时消息**安全滞留队列**,不丢失、不投递 | [源码确认] | 对无人值守语义关键:消息不会投进真空 |
| main 分支新增 `--resume`(排队后让 daemon resume 线程,实现无 TUI 投递;0.164.0 **无**此 flag,约 0.165+) | [源码确认] | PR #57232(2026-10-26);v1 不使用,作为未来增强记录 |
| 引入版本:首个稳定版 **0.149.0**(2026-08-20) | [文档确认] | GitHub release notes + PR #39092 |
| 与受管 daemon 的 `-c` 覆盖冲突(配置覆盖强制内嵌 server 且存在 daemon 时报错) | [参考] | 我们不传 `-c`,天然规避;L3 复核 |

### 2.2 `codex app-server` 与 `account/rateLimits/read`

| 事实 | 级别 | 依据 |
| --- | --- | --- |
| 子命令存在:`codex app-server`(stdio 服务)、`daemon`、`proxy`、`generate-json-schema` 等 | [实证] | 0.162.1 `--help` |
| 协议:**类 JSON-RPC 但无 `jsonrpc` 字段**(源码注释明示);请求 `{id,method,params?}`、响应 `{id,result}`/`{id,error}`、通知 `{method,params?}`;**stdio 帧格式 = 换行分隔 JSON**(WebSocket 为每帧一条) | [源码确认+参考] | `app-server-protocol/src/rpc.rs`、`app-server-transport/src/transport/stdio.rs` |
| `initialize` 握手:params `clientInfo{name,version}`(+可选 capabilities,camelCase);响应 `{userAgent, codexHome, platformFamily, platformOS}`;随后发 `initialized` 通知;未初始化的请求被拒;**实验方法(如 `thread/queue/*`)需 `capabilities.experimentalApi:true`** | [源码确认] | 我们只用稳定只读方法,仍按参考项目声明该能力 |
| 按行上限 ~1MiB、每调用超时(参考项目 25s);服务端反向请求可拒 -32601 | [参考] | 参考项目 appserver.py |
| 方法总数(0.162.1 schema 导出)105 个;main 分支约 170 个 client request;`account/rateLimits/read` 名称与大小写**双重确认** | [实证+源码确认] | schema ClientRequest.json;`common.rs:1360` |
| 响应窗口字段:`usedPercent`(必填 int)、`resetsAt`(int64 秒可空)、`windowDurationMins`(可空);无 window_type(线上以 primary/secondary 表达窗口种类) | [实证+源码确认] | schema RateLimitWindow;`v2/account.rs:343` |
| 响应顶层:`ordinaryUsageAllowed`(bool|null,"clients must not infer recovery from percentages or reset times")、`rateLimits`、`rateLimitsByLimitId`(可空 dict,多桶)、`rateLimitResetCredits{availableCount,credits[]}`、`accountId` | [实证+源码确认] | schema + 源码响应结构 |
| 枚举:PlanType 17 值;RateLimitReachedType 5 值;ResetCreditStatus = available/redeeming/redeemed/unknown | [实证] | schema |
| 通知 `account/rateLimits/updated` 存在(服务端可推送额度更新);推送时机/订阅条件 [待核-L3 观察] | [实证 schema] | — |
| Linux 上 `codex app-server --stdio` 常驻子进程稳定性;`account/rateLimits/read` 是否每次打后端(本地缓存 TTL 未知) | [待核] | L2/L3 |

### 2.3 rollout JSONL(sessions/)

| 事实 | 级别 |
| --- | --- |
| 路径 `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`;另有 `archived_sessions/`;分裂链命名 `<A>_<B>.jsonl` [参考] | 实证(命名/目录)+参考(分裂链) |
| 顶层事件:`session_meta`/`turn_context`/`response_item`/`event_msg`/`token_usage_record`/`world_state` | [实证] |
| `event_msg.payload`: `token_count`(携带 `rate_limits`,snake_case)、`task_started`(`turn_attribution.turn_trigger`)、`task_complete`(`turn_id`/`error`)、`item_completed` 等 | [实证] |
| `token_count.rate_limits` 按窗口含 primary/secondary,字段 `used_percent/resets_at/window_minutes`;实测捕获五小时窗 100% 真实样本 | [实证] |
| 用户消息 = `response_item.payload.type == "message"` 且 `role=="user"`;queue 投出的消息同形态入库(按 marker 区分) | [实证+参考] |
| **轮次来源判别(源码确认)**:`event_msg.task_started.turn_attribution.turn_trigger` —— TUI 人工输入 = `"user"`,队列派发 = `"queue"`,守护评审 = `"guardian_review"` 等;另 `~/.codex/history.jsonl` 只记录 TUI 手工输入,可作人工消息交叉验证 | [源码确认+实证] |
| 失败错误标识:历史库 `error_json.codexErrorInfo == "usageLimitExceeded"`;JSONL `task_complete.error.codex_error_info == "usage_limit_exceeded"`(两处拼写不同) | [参考] |
| 额度失败可在 primary=98% 时发生(服务器预扣)→ 支持 99% 阈值的保守设计 | [参考] |

### 2.4 Codex 自带 SQLite 跨版本对照

| 版本/形态 | 存储 | 结论 |
| --- | --- | --- |
| 0.162.1 Linux CLI(本机)[实证] | `~/.codex/` 根目录:`thread_history_1.sqlite`(101M,表 thread_turns/thread_items/thread_history_projection_state)、`queue_1.sqlite`(queued_items/queued_thread_revisions)、`state_5.sqlite`、`logs_2.sqlite`;另有 `sqlite/codex.db`(automations/inbox/local_thread_catalog)、`history.jsonl`(TUI 手工输入历史)、`session_index.jsonl` | 结构化三库在 CLI 上同样存在;初测误判已更正(evidence §4) |
| 0.164.x Windows 桌面(参考项目)[参考] | 同构三库(代数后缀),另以 Restart Manager 探测会话加载状态 | 检测/确认/队列观察主源 |
| 0.164.0 Linux CLI(生产目标) | 同代家族,极大概率同构 | L2 doctor 探测闭环;设计按"有/无"自适应,无结构库时回退 JSONL |

### 2.5 版本情况与安装

- **当前最新稳定版:0.164.0**(2026-10-23 发布,GitHub "Latest";其后为 0.165/0.166 alpha)。生产目标 0.164.0 即最新稳定版。[文档确认]
- `codex queue` 引入于 **0.149.0**(2026-08-20);app-server 实验 thread/queue API 更早(0.148.0)。本机 0.162.1 含全部相关能力。
- 近期相关变更(无正式 Breaking 标注):0.164.0 修复排队消息等待权限选择的行为、app-server WebSocket zstd 协商;main 分支 `codex queue` 新增 `--resume`(无人值守拉起投递,约 0.165+,v1 不用);**额度协议(`account/rateLimits/read`、RateLimitSnapshot 字段)近期无破坏性变更记录**。
- 安装途径:官方安装脚本 `curl -fsSL https://chatgpt.com/codex/install.sh | sh`、npm `@openai/codex`、Homebrew cask、GitHub Releases 二进制、源码编译。[文档确认 README]
- `CODEX_HOME` 语义(源码确认):指定状态根目录;**设置时目录必须已存在**(否则报错),未设默认 `~/.codex`;`CODEX_SQLITE_HOME` 可单独重定向 SQLite 目录(发现逻辑需兼容)。

### 2.6 其他相关命令(一句话)

- `codex resume`:在 TUI 中恢复既有会话(人用);`codex exec`:非交互一次性执行——均不在 v1 自动化路径中(未来扩展再评估)。

## 3. 参考项目分析(codex-auto-resume-windows)

### 3.1 项目概况

- Windows 10/11 的 Codex 用量限额自动续作工具(v0.6.19),Python 主逻辑 46.5k 行 + C# GUI + 安装器;**MIT 许可**(允许复用,保留版权声明即可)。
- 数据面:直接只读 Codex 本地 SQLite(`thread_history/queue/state`)+ JSONL;控制面:`codex queue` 投递 + `codex app-server` 只读额度/撤回队列。

### 3.2 值得借鉴并已纳入设计的机制

| 机制 | 参考实现要点 | 本项目落地 |
| --- | --- | --- |
| 失败分类表 | `error_json.codexErrorInfo` 变体 → 类别;unknown 永不重试 | recovery §4.2(分类表摘录) |
| 额度双表示解析 | App Server camelCase / JSONL snake_case;`rateLimitsByLimitId` 分桶;blocked=max(resets_at);90-99 hint | quota §1/§5 |
| 恢复时间分层 | 现场探测 > 失败前快照 > 保守轮询;reset+60s 宽限 | quota/early-recovery |
| 投递协议 | `--message=` 单元素形式;回执正则;**spawn 后一切不确定为终态**;仅 spawn 失败可退避重试(≤5);45s 超时;回执只记分类码不记原文 | queue-safety §1/§4 |
| marker 幂等 | 消息尾 `[codex-auto-resume:<16hex>]`;队列库/历史库双查 marker;interruption_id 内容哈希去重 | queue-safety §3/§6(marker 改为本项目前缀) |
| claim 先落库 | 投递前置 submitting 持久化,崩溃后对账 | persistence §5/§7(attempting 协议) |
| 门禁次序 | consent→schedule→engine→single-owner→in-flight→budget→thread→newer-work→caps→usage 现场复查 | queue-safety G1..G12 |
| 节流纪律 | 每线程每日 5 条上限、900s 冷却、单会话单在途 | G12/配置(上限采纳为默认) |
| App Server 客户端 | NDJSON、白名单方法表、拒绝服务端请求 | architecture §5、quota §4 |
| 只读纪律 | Codex SQLite `mode=ro`+`query_only`+`trusted_schema=OFF` | recovery §4.1 |

### 3.3 不采纳 / 必须重设计的部分

1. **Windows 平台层**(Restart Manager 锁探测、PowerShell 进程清单、WinRT 通知、注册表自启、`platformOs=="windows"` 断言)——全部替换为 Linux 等价(flock、/proc、journald、systemd)。
2. **每次探测 spawn 一个 app-server 进程**——改为常驻子进程(风险 R-04 附带验证)。
3. **`-c` 遥测关闭参数与 `--remote unix://` 路由**——v1 纯净 argv,不带覆盖(§2.1 待核项,避免配置覆盖兼容矩阵)。
4. **`thread/queue/delete` 撤回**——写操作,v1 不启用(可选增强,见 queue-safety §7)。
5. **GUI/安装器/双版本体系/Advanced headless 路线**(`thread/resume`+`turn/start` 直接驱动)——超范围;headless 作为远期扩展记录。
6. **其 `loaded` 判定依赖桌面 App 加载会话** —— Linux CLI 场景语义不同,G7 以 rollout 末尾事件 + (可用时)app-server 线程状态判定,不确定即不投。

### 3.4 许可与借鉴声明

参考项目为 MIT。本项目 v1 **不直接复制其代码**,借鉴其协议事实与安全机制设计(上表);若未来引入实质代码段落,将按 MIT 要求在其文件头保留其版权声明并在 NOTICE 中注明。

## 4. 数据源能力矩阵(检测/确认层输入)

| 能力 | 0.162.1 CLI | 0.164 桌面(参考) | 0.164 CLI(待核) | 无库时回退 |
| --- | --- | --- | --- | --- |
| 失败检测 | JSONL(task_complete.error + token_count) | thread_history.thread_turns | doctor 探测 | JSONL 恒可用 |
| 人工消息检测 | JSONL user message(无 marker) | thread_items.userMessage | 同左 | JSONL |
| 队列在途观察 | ✗(无 queue 库) | queued_items 含 marker 行 | 待核 | 跳过 C1,靠 C2 |
| 轮次开始确认 | JSONL marker user item | thread_items marker 行 | 同左 | JSONL |
| 实时额度 | app-server | app-server | app-server | JSONL 历史(降级,标非实时) |

## 5. 风险登记册

| # | 风险 | 影响 | 概率 | 缓解 | 闭环手段 |
| --- | --- | --- | --- | --- | --- |
| R-01 | 回执/字段格式随版本漂移(queue 回执是英文单行文本,未承诺稳定) | 投递全部降为 unknown(安全但不可用) | 中 | 解析器版本化;doctor L2 检测回执形状;变更即通知 | T-COMPAT + doctor |
| R-02 | `initialize` 握手要求(v2 客户端能力/实验开关)不明 | app-server 客户端连不上 | 中 | 按参考项目参数实现;失败审计 | L2 |
| R-03 | `thread_history/queue` 库 Linux CLI 不存在 | 结构化检测/队列观察不可用 | 已确认存在此差异 | 双源自适应设计(§4) | L2 探测 |
| R-04 | 受管 app-server daemon 与 queue 的交互(daemon 缺失/旧版 daemon 不支持 `thread/queue/add`/`-c` 覆盖冲突) | 特定环境投递失败或拒绝 | 低(我们不传 `-c`,天然规避覆盖冲突;daemon 缺失时 queue 明确报错→unknown,fail-closed) | doctor L2 增加 daemon 探测;L3 复核 | L2/L3 |
| R-05 | ~~turn_trigger 取值未实证~~ 已闭环 | — | 已确认 `"user"`/`"queue"`/`"guardian_review"`(源码+实证);用于 G3/C2 判据 | — |
| R-12 | 消息已入队但线程未加载(TUI 关闭过夜/周末)→ 长期滞留 | 确认轮询长期等待;用户以为"没恢复" | C1 正证据(队列库含 marker 行)存在时**不触发确认超时**,改发提醒("已排队,等待会话加载");此语义已写入 recovery §6 | L3 |
| R-06 | JSONL 大文件/高频写下的增量读取效率 | tick 变慢 | 低 | 字节游标增量;NFR-07 性能测试 | T-PERF |
| R-07 | 额度快照 `ordinaryUsageAllowed` 恒为 null(部分计划类型) | 第三道证据缺失 | 中 | 设计允许 null 降级(记录于 evidence),前两道证据仍强制 | L2/L3 观察实际值 |
| R-08 | 多账户(CODEX_HOME)下 app-server 子进程资源占用 | 内存/进程数增长 | 低 | 空闲回收;账户数实际很少 | T-PERF |
| R-09 | Debian 13(生产)与开发机行为差异(systemd/polkit/linger) | 部署受阻 | 低 | doctor L0 覆盖;CI 容器矩阵 | L2/L4 |
| R-10 | 本项目许可证(MIT 占位)未最终确认 | 发布阻塞 | 低 | 发布前向所有者确认 | P7-T3 |
| R-11 | Codex 并发写 SQLite 与我们只读打开的锁交互 | 读取偶发 busy | 低 | WAL 只读 + busy_timeout + 短查询 | L2/L4 观察日志 |

## 6. 对设计文档的反哺(已完成的回填)

本报告的实证结果已写入:queue-safety.md(argv 形式/回执正则/outcome 分类/marker)、quota-monitoring.md(双命名/分桶/ordinaryUsageAllowed/客户端协议)、recovery-state-machine.md(双源检测/分类表/确认三级证据)、persistence.md(投递协议枚举)、testing.md(T-COMPAT 形状清单)。
