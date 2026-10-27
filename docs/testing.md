# 测试计划(testing.md)

| 项 | 值 |
| --- | --- |
| 状态 | Phase 1 定稿 v1.0 |
| 覆盖需求 | 任务书 §六(三级测试分离)、NFR-05、requirements.md §7(L1–L4) |
| 关联 | 全部设计文档(测试锚点) |

---

## 1. 三级测试分离(铁律)

| 级别 | 内容 | 环境 | 红线 |
| --- | --- | --- | --- |
| 本地自动化(L1 单元+集成) | 全部 S 场景,用 Fake | 开发机/CI | **绝不**调用真实 codex 二进制投递、**绝不**读开发者真实 `~/.codex` 的会话内容(fixture 全部人工构造/脱敏) |
| 模拟集成(L1 的一部分) | Fake Codex CLI + Fake App Server + 临时 JSONL/DB + 假时钟,组装全链路 | 同上 | 同上;不为过测试而消耗真实额度 |
| 真实环境验收(L2/L3/L4) | 见 §6 清单 | 目标机,用户授权 | L2 只读;L3 仅向**专用测试会话**投递;L4 才触生产 |

**任何测试结论必须附证据**(pytest 输出、doctor 输出、命令与时间),文档/提交信息中注明级别(任务书执行规则 15)。

## 2. 测试架构(Fake 体系)

```
tests/
├── unit/                  # 纯逻辑:解析器、状态机、守门、判定、模板、去重
├── integration/           # 组装:Fake CLI/AppServer + tmp 目录 + 假时钟,跑 tick/崩溃/恢复
├── compatibility/         # Codex 版本兼容:多版本字段形状 fixture 驱动解析器
├── fakes/
│   ├── codex_cli.py       # 伪 codex 二进制(可执行):queue 回执/挂起/超时/非零退出/回显 prompt(--behaviors)
│   ├── app_server.py      # 伪 app-server:NDJSON、initialize 握手、rateLimits 场景剧本(--script)
│   └── clock.py           # 假时钟(可拨动、单调)
└── fixtures/
    ├── jsonl/             # rollout 片段(脱敏/人工):正常、5h99/100、weekly、双窗口、撕裂行、task_complete+error…
    ├── appserver/         # rateLimits 响应样本(camelCase 新旧形状、rateLimitsByLimitId、多桶、错误)
    └── codexdb/           # thread_history/queue SQLite 样本(用 sqlite3 构造脚本生成)
```

- **注入点即测试点**:Scheduler/StateMachine/DeliveryManager 只依赖端口接口,测试注入 Fakes(architecture.md §1 的结构保证)。
- 假时钟:`TimeSource` 注入,所有超时/节奏/新鲜度判定可确定性快进;DST/跨时区用例注入相应本地时区(NFR-04)。
- Fake codex CLI 通过 `argv[0]` 指向测试目录的脚本实现;配置 `codex.binary_path` 本就支持覆盖(生产亦然)。
- **Phase 2 已交付**:`tests/fakes/clock.py`(FakeClock)、`tests/conftest.py`(HOME/XDG 隔离 autouse fixture,L1 红线的结构保证)、`tests/unit/test_source_scan.py`(T-GUARD-04 部分与 T-STORE-03 审计仅 INSERT)。Fake codex CLI 与 Fake app-server 随 C 组任务(P3-T3/P5-T1)交付。

## 3. 场景矩阵(S↔测试 ID)

单元/集成测试组前缀:`T-PARSE`(解析)、`T-SM`(状态机)、`T-GUARD`(守门/安全)、`T-DEL`(投递)、`T-CONF`(确认)、`T-QUOTA`(额度判定)、`T-STORE`(持久化)、`T-CLI`(命令)、`T-NOTIFY`(通知)、`T-SCHED`(调度/锁)、`T-CRASH`(崩溃)、`T-COMPAT`(兼容)、`T-PERF`(性能)。

| 场景 | 测试 | 关键断言 |
| --- | --- | --- |
| S-01 5h 100% | T-QUOTA-01、T-SM-01、T-DEL-01(全链路集成) | 失败建账→等待→窗口滚动+实时复查→投递一次→marker 确认 |
| S-02 5h 99% | T-QUOTA-02 | 99 即 exhausted;与 100 同路径 |
| S-03/04 weekly | T-QUOTA-03/04 | secondary(10080)判定 |
| S-05 双窗口 | T-QUOTA-05、T-SM-02 | 一窗解除不投;全解除才投;调度参考=max(resets_at) |
| S-06 官方提前重置 | T-SM-03、T-DEL-02 | E2 信号→强制实时复查→守门→投递;`early_recovery_detected` |
| S-07 用户兑换重置券 | T-QUOTA-06、T-GUARD-06 | E3 信号只观察 credits 状态;黑名单断言 consume 从未被调用 |
| S-08 App Server 失败 | T-QUOTA-07 | 降级 jsonl 展示并标非实时;该账户评估=unknown,不投递 |
| S-09 多账户 | T-STORE-01、T-QUOTA-08 | 快照隔离;查询按账户各一次 |
| S-10 多会话 | T-SCHED-01 | 状态互不影响;单会话异常隔离 |
| S-11 queue 成功 | T-DEL-01 | accepted+queue_id 落库;回执正则 |
| S-12 queue 无响应 | T-DEL-03 | 挂起→超时 kill→unknown→NEEDS_HUMAN |
| S-13 发送后超时 | T-DEL-04 | 非零退出/空输出→unknown;不重发 |
| S-14 人工提前恢复 | T-CONF-02 | 不含 marker 的 user 消息→SUPERSEDED+通知;不投/已排队不撤(v1) |
| S-15 会话不存在 | T-SM-04 | MISSING;宽限期后 NEEDS_HUMAN;绝不建进程 |
| S-16 SQLite 崩溃恢复 | T-CRASH-01/02/03 | kill -9 于崩溃矩阵各时点(persistence §7 表)→重启清扫结果逐格断言;恒不双投 |
| S-17 重复调度器 | T-SCHED-02 | 第二实例 flock 拒绝,退出码 21 |
| S-18 网络异常 | T-QUOTA-07、T-NOTIFY-02 | 退避节奏;不崩;恢复后正常 |
| S-19 通知失败 | T-NOTIFY-03 | SMTP 不可达→重试→dead;主流程不受阻 |
| S-20 额度未知 | T-QUOTA-09 | unknown 阻断 G6;quota_unknown 策略 manual_ack |
| S-21 不可恢复错误 | T-STORE-02 | 库损坏→受限模式;doctor 报告;退出码 20/30 |
| S-22 人工消息 supersede | T-CONF-02 | 见 S-14,另含"失败后任何时刻"变体 |
| S-23 撕裂行 | T-PARSE-01 | 忽略+警告;下一 tick 补读 |
| S-24 暂停 | T-SM-05 | 全局/单会话暂停不投;解除后守门通过才投 |
| S-25 未知窗口类型 | T-COMPAT-01 | unknown 桶保守阻塞 |
| S-26 任务完成 | T-SM-06 | task_complete→通知,不建失败 |
| S-27 确认超时 | T-CONF-03 | 30 分钟无 marker→NEEDS_HUMAN;不重投 |
| S-28 运行中轮次 | T-GUARD-07 | G7:未闭合轮→不投;不确定→不投 |

## 4. 安全测试组(T-GUARD,FR-07/NFR-01/03/09)

| ID | 内容 |
| --- | --- |
| T-GUARD-01 | CommandRunner 拒绝非白名单 argv(模糊测试:随机 flag/子命令/`-` 开头消息/环境变量注入) |
| T-GUARD-02 | 静态扫描:黑名单方法字符串零出现;app-server 请求构造仅存在于具名白名单方法内 |
| T-GUARD-03 | 配置注入:全部配置字段灌入恶意值,可执行命令集不变 |
| T-GUARD-04 | 硬编码扫描:真实 UUID、`/home/`、QQ 邮箱域、版本号字面量零出现 |
| T-GUARD-05 | 隐私:代码无 `auth.json` 读取路径;日志/通知无会话正文 |
| T-GUARD-06 | 重置券:全集成剧本中 Fake App Server 记录全部收到的方法,断言仅白名单只读方法出现 |

## 5. 兼容性测试(T-COMPAT)

fixture 驱动的形状测试,覆盖已知版本差异(证据见 phase0-research.md):

- App Server 响应:camelCase `usedPercent/windowDurationMins/resetsAt`、`rateLimitsByLimitId` 有/无(回退 `rateLimits`)、多桶(codex/premium/legacy)、`ordinaryUsageAllowed` true/false/null;
- JSONL:snake_case `used_percent/window_minutes/resets_at`、`limit_id` 分桶、撕裂尾行、分裂链文件名、`task_complete.error.codex_error_info` 两种拼写;
- thread_history 库:有/无(0.162.1 CLI vs 0.164 桌面)自动降级;
- queue 回执:标准行/变体(前后空白)不识别→unknown。

## 6. L2/L3/L4 真实环境验收(用户在目标机执行)

### L2:本机接口探测(`codex-autopilot doctor`,全只读)
1. `codex --version` 记录;`codex queue --help` 含 `--thread`/`--message`;
2. `codex app-server --stdio` 握手 + `account/rateLimits/read` 返回可解析(记录实际形状,回填 T-COMPAT fixture);
3. sessions 目录可读、注册会话 rollout 可解析;`thread_history/queue` 库存在性探测;
4. 产出:doctor 报告(附实际响应样本,脱敏)。

### L3:隔离测试会话验证(用户授权,仅专用会话)
1. 建一次性测试会话(与生产无关);
2. `codex-autopilot add`(auto_resume 先关)→ 观察检测/展示;
3. 开 auto_resume → 在测试会话上人工触发一次真实 `check` 投递 → 确认 marker 轮次开始(此步消耗一次真实消息,需用户明确同意);
4. (顺带)验证真实额度耗尽→恢复:不可强求,以运行记录自然积累。

### L4:生产迁移验收(见 migration-from-legacy.md §6)
1. 并行观察期 ≥7 天:autopilot(不投递)vs 旧脚本行为对照;
2. 逐会话开启 auto_resume;首次真实恢复留存全链路证据(audit/history/通知);
3. 退役旧 timer。

## 7. CI

GitHub Actions(`.github/workflows/ci.yml`,Phase 2 起生效):矩阵 Python 3.11/3.12/3.13 × 单元+集成;ruff lint + format;wheel 构建 + 安装冒烟。L1 容器矩阵(debian:stable、ubuntu:latest、archlinux:base)跑安装冒烟(pipx install + doctor --json 退出码)随 P6-T4 落地;T-GUARD/T-COMPAT 全量。覆盖率门槛建议:领域层(quota/recovery/scheduling)≥90%,端口层 ≥70%。

## 8. 证据记录约定

- 测试报告存 `docs/evidence/`(CI artifact 或 doctor 输出,脱敏);
- 每份证据注明:日期、机器、级别(L1/L2/L3/L4)、命令、退出码;
- 禁止把 L1 结果表述为 L3/L4(任务书 §六)。
