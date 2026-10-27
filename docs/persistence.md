# 持久化设计(persistence.md)

| 项 | 值 |
| --- | --- |
| 状态 | v1.1(Phase 2 实现;§2 PRAGMA 经安全复审修订,见 §2.1 / D-PER-7) |
| 覆盖需求 | FR-09、FR-08-A7、NFR-06 |
| 关联 | [session-management.md](session-management.md)(实体语义)、[queue-safety.md](queue-safety.md)(投递协议) |

---

## 1. 选型决策

**决策:SQLite(WAL 模式),`sqlite3` 标准库,无 ORM,手写编号迁移。**

| 方案 | 结论 | 理由 |
| --- | --- | --- |
| SQLite + 原生 SQL | ✅ 采用 | 单文件、原子事务、崩溃恢复成熟、随 Python 免安装;WAL 允许守护进程写 + CLI 读并发 |
| JSON 文件 + 原子替换 | ❌ | 无并发安全、无外键、无唯一约束;"严格设计的原子持久化"在多实体关系面前不可靠 |
| PostgreSQL | ❌ | 违背"独立可安装的开源工具"定位,要求用户运维额外服务 |
| SQLAlchemy 等 ORM | ❌ | 迁移与 SQL 不可直读,审计困难;本项目数据量小,手写 SQL 全部可测 |

存储位置:`$XDG_STATE_HOME/codex-autopilot/state.db`(默认 `~/.local/state/codex-autopilot/state.db`),配置可覆盖(见 cli-design.md §2)。

## 2. 连接与并发纪律

- 每进程一个连接(守护进程、CLI 各自);`check_same_thread=False` 仅在单线程事件循环内使用(见 architecture.md §4)。
- PRAGMA(打开时设置,存入连接钩子):
  - `journal_mode=WAL` —— 读写不互斥;
  - `synchronous=FULL` —— **Phase 2 复审后由 NORMAL 调整为 FULL**(每次 COMMIT fsync WAL;论证见 §2.1。写入量每 tick 数条,FULL 的代价可忽略;该值不暴露为配置项——配置不可弱化安全性);由测试 `test_synchronous_full_is_enforced` 与源码扫描锁定;
  - `busy_timeout=5000`;`foreign_keys=ON`。
- **写事务模板**:所有多语句写一律 `BEGIN IMMEDIATE` → … → `COMMIT`,失败 `ROLLBACK`。禁止隐式事务。
- CLI 与守护进程同时写:靠 IMMEDIATE + busy_timeout 串行化;CLI 写操作(如 pause)为短事务,冲突概率低且可重试。
- 单实例锁:**守护进程启动时对 `$state_dir/daemon.lock` 取 `flock(LOCK_EX|LOCK_NB)`**,失败即退出并提示(场景 S-17);`tick`/`check` 同样短暂持锁(评估期间),避免与守护进程并发投递。锁文件不保证跨 NFS(文档注明仅支持本地文件系统)。

### 2.1 持久化安全性复审:为什么 synchronous 必须是 FULL(Phase 2 结论)

原 v1.0 设计采用 `synchronous=NORMAL`,理由是"掉电最多丢最后一个事务,投递协议 §7 会把任何丢失收敛为 unknown → needs_human"。Phase 2 实现时复审发现该论证有一处错误、一处低估:

1. **论证错误**:`attempting` 先落库(§5 步骤 3)→ COMMIT 返回 → spawn `codex queue`。在 NORMAL 下 COMMIT 不同步 fsync WAL,**COMMIT 返回 ≠ 记录已持久**。若掉电发生在"COMMIT 已返回、WAL 尚未 fsync"的窗口,且 spawn 的队列消息已经发出(外部副作用不可回滚),重启后数据库中没有这条投递记录:守门重读看不到 deliveries 行、failure 仍 open → 重新评估 → **第二次投递同一失败**。`UNIQUE(failure_id, attempt_no)` 无法拦截——被丢掉的行已不存在。这直接违反 FR-05-A5(每失败至多一次成功投递)与 FR-08-A7。原论证只考虑了"attempting 已丢失 → 收敛 unknown"的安全方向,漏掉了"spawn 已发生但登记丢失 → 双投"的不安全方向。
2. **低估**:NORMAL 的丢失窗口不是"最后一个事务",而是**自上次 WAL fsync 以来的全部已提交事务**(检查点之前的整段)。同理,投递门控状态(`runtime_flags.global_pause`、`sessions.enabled/auto_resume`)在该窗口内丢写,同样可能造成"用户以为已暂停、工具却投递了"的事故——这不是投递协议能兜底的。

**决策(D-PER-7)**:`synchronous=FULL`,不可配置。FULL 在 WAL 模式下每次 COMMIT 都 fsync WAL,"attempting 已落库"在 spawn 前即为持久事实;所有门控状态写入即时持久。代价是每次写事务一次 fsync——本工具写负载为每 tick 数条、远低于性能预算(NFR-07 < 5s/tick),不值得为省几毫秒引入双投风险。剩余风险:commit 之后的 outcome 更新若丢失,只会多出一条 `attempting` → 启动清扫转 unknown → needs_human(fail-closed,安全方向)。

## 3. 模式(Schema)v1

时间戳统一 Unix 秒(UTC,INTEGER);JSON 字段存 TEXT。`--` 注释为逻辑说明。

```sql
-- 迁移框架:每个迁移一个编号,只增不改
CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  INTEGER NOT NULL
);

-- 运行期标志(全局暂停等)
CREATE TABLE runtime_flags (
    key         TEXT PRIMARY KEY,        -- 如 'global_pause'
    value       TEXT NOT NULL,           -- JSON
    updated_at  INTEGER NOT NULL
);

-- 账户 = 一个 Codex Home
CREATE TABLE accounts (
    id          INTEGER PRIMARY KEY,
    codex_home  TEXT NOT NULL UNIQUE,    -- realpath 规范化后的绝对路径
    label       TEXT,
    created_at  INTEGER NOT NULL
);

-- 注册会话(语义见 session-management.md §1.3)
CREATE TABLE sessions (
    uuid            TEXT PRIMARY KEY,    -- canonical 小写 UUID
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    project_path    TEXT NOT NULL,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    enabled         INTEGER NOT NULL DEFAULT 1,
    auto_resume     INTEGER NOT NULL DEFAULT 0,   -- 安全默认:关
    tmux_session    TEXT,
    resume_template TEXT,                -- NULL=用全局模板
    notify_policy   TEXT,                -- JSON, NULL=用全局策略
    check_interval  INTEGER,             -- 秒, NULL=用全局
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    deleted_at      INTEGER              -- 软删除
);
CREATE INDEX idx_sessions_account ON sessions(account_id) WHERE deleted_at IS NULL;

-- 当前状态缓存(状态机详情见 recovery-state-machine.md)
CREATE TABLE session_state (
    session_uuid TEXT PRIMARY KEY REFERENCES sessions(uuid),
    state        TEXT NOT NULL,          -- ACTIVE/WAITING_RECOVERY/DELIVERING/...
    since        INTEGER NOT NULL,
    detail       TEXT                    -- JSON:状态附属信息(如 plan_id)
);

-- 状态迁移历史(append-only)
CREATE TABLE state_history (
    id          INTEGER PRIMARY KEY,
    session_uuid TEXT NOT NULL REFERENCES sessions(uuid),
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    reason      TEXT NOT NULL,           -- 稳定机器码,如 'quota_window_cleared'
    evidence    TEXT NOT NULL,           -- JSON:触发迁移的证据(快照 id/事件摘要)
    ts          INTEGER NOT NULL
);
CREATE INDEX idx_state_history_session ON state_history(session_uuid, ts);

-- 额度快照(账户级;来源与新鲜度见 quota-monitoring.md)
CREATE TABLE quota_snapshots (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    source      TEXT NOT NULL,           -- 'appserver' | 'jsonl'
    captured_at INTEGER NOT NULL,        -- 数据自身时间
    ingested_at INTEGER NOT NULL,        -- 入库时间
    ok          INTEGER NOT NULL,        -- 0=解析失败(错误快照也留痕)
    error       TEXT,                    -- 失败原因
    raw         TEXT,                    -- 原始 JSON(截断上限,见 §8)
    parser_version TEXT NOT NULL
);
CREATE INDEX idx_snapshots_account ON quota_snapshots(account_id, captured_at DESC);

-- 快照的窗口明细(一行一个窗口)
CREATE TABLE quota_windows (
    id              INTEGER PRIMARY KEY,
    snapshot_id     INTEGER NOT NULL REFERENCES quota_snapshots(id) ON DELETE CASCADE,
    bucket_key      TEXT NOT NULL,       -- 稳定标识:window_minutes 或 'unknown:n'
    window_kind     TEXT NOT NULL,       -- 'five_hour'|'weekly'|'credits'|'spend_control'|'unknown'
    kind_inferred   INTEGER NOT NULL DEFAULT 0,  -- 1=按位置推断(FR-03-A2)
    used_percent    REAL,
    resets_at       INTEGER,             -- Unix 秒,可空
    window_minutes  INTEGER,
    is_exhausted    INTEGER NOT NULL DEFAULT 0   -- ≥阈值
);

-- 失败轮次
CREATE TABLE failures (
    id              INTEGER PRIMARY KEY,
    session_uuid    TEXT NOT NULL REFERENCES sessions(uuid),
    turn_id         TEXT NOT NULL,       -- rollout 中的轮次标识
    kind            TEXT NOT NULL,       -- 'quota_five_hour'|'quota_weekly'|'quota_multi'|
                                        -- 'quota_credits'|'quota_spend_control'|'quota_unknown'|'aborted'
    detected_at     INTEGER NOT NULL,
    evidence        TEXT NOT NULL,       -- JSON:触发判定的 rollout 事件摘要+快照引用
    blocking_window_keys TEXT NOT NULL,  -- JSON 数组:当时阻塞的 bucket_key 列表
    status          TEXT NOT NULL DEFAULT 'open'
                    -- 'open'→'planned'→'delivering'→'delivered'→'confirmed'|'needs_human'
                    -- | 'superseded'(人工介入)| 'cancelled'|'expired'
    closed_at       INTEGER,
    UNIQUE(session_uuid, turn_id, detected_at)  -- 同一轮次同一时刻只记一次
);

-- 恢复计划(一个失败至多一个活动计划)
CREATE TABLE recovery_plans (
    id          INTEGER PRIMARY KEY,
    failure_id  INTEGER NOT NULL UNIQUE REFERENCES failures(id),
    strategy    TEXT NOT NULL,           -- 'window_reset' | 'manual_ack'
    not_before  INTEGER NOT NULL,        -- 最早重新评估时间(调度参考,非投递授权)
    basis       TEXT NOT NULL,           -- JSON:依据(各窗口 resets_at、阈值、余量)
    status      TEXT NOT NULL DEFAULT 'waiting',
                -- 'waiting'|'cancelled'|'delivered'|'expired'
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

-- 投递尝试(协议详见 queue-safety.md §5)
CREATE TABLE deliveries (
    id          INTEGER PRIMARY KEY,
    failure_id  INTEGER NOT NULL REFERENCES failures(id),
    attempt_no  INTEGER NOT NULL,        -- 1 起;自动路径恒为 1
    argv        TEXT NOT NULL,           -- JSON 数组(实际执行的 argv,留痕)
    started_at  INTEGER NOT NULL,
    ended_at    INTEGER,
    outcome     TEXT NOT NULL DEFAULT 'attempting',
                -- 'attempting'→'accepted'|'unknown'|'not_started'
                -- (spawn 失败=not_started,唯一可重试类;语义见 queue-safety.md §4)
    receipt     TEXT,                    -- JSON:stdout/stderr/退出码/耗时
    UNIQUE(failure_id, attempt_no)
);
CREATE INDEX idx_delivers_failure ON deliveries(failure_id);

-- 恢复确认
CREATE TABLE confirmations (
    id          INTEGER PRIMARY KEY,
    delivery_id INTEGER NOT NULL UNIQUE REFERENCES deliveries(id),
    method      TEXT NOT NULL,           -- 'message_match'|'new_turn'|'manual_ack'|'timeout'
    confirmed_at INTEGER,
    evidence    TEXT NOT NULL,           -- JSON:确认证据(匹配到的 rollout 事件摘要)
    created_at  INTEGER NOT NULL
);

-- 通知记录与去重(notifications.md)
CREATE TABLE notification_log (
    id          INTEGER PRIMARY KEY,
    dedup_key   TEXT NOT NULL,
    channel     TEXT NOT NULL,           -- 'console'|'smtp'
    event       TEXT NOT NULL,           -- 事件类型
    session_uuid TEXT,
    status      TEXT NOT NULL,           -- 'sent'|'suppressed'|'failed'|'dead'
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_attempt_at INTEGER,
    sent_at     INTEGER,
    payload     TEXT,                    -- JSON
    UNIQUE(dedup_key, channel)
);

-- 审计(append-only,不可变;NFR-06)
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL,
    actor       TEXT NOT NULL,           -- 'daemon'|'cli:<command>'|'recovery'
    category    TEXT NOT NULL,           -- 'tick'|'quota'|'state'|'delivery'|'notify'|'fault'|'admin'
    level       TEXT NOT NULL,           -- 'info'|'warn'|'error'
    session_uuid TEXT,
    summary     TEXT NOT NULL,           -- 一行人类可读
    data        TEXT                     -- JSON:结构化细节(不含会话内容)
);
CREATE INDEX idx_audit_ts ON audit_log(ts);
CREATE INDEX idx_audit_session ON audit_log(session_uuid, ts);
```

## 4. 关键不变量(数据库层兜底,代码层之外的第二道防线)

| 不变量 | 机制 |
| --- | --- |
| 同一 UUID 只注册一次 | `sessions.uuid` PRIMARY KEY |
| 一个失败至多一个活动计划 | `recovery_plans.failure_id` UNIQUE |
| 一个失败至多一次投递尝试(自动路径) | `UNIQUE(failure_id, attempt_no)` + 代码只在 `attempt_no=1` 且无既有行时插入 |
| 一次投递至多一条确认 | `confirmations.delivery_id` UNIQUE |
| 通知不重复 | `UNIQUE(dedup_key, channel)` |
| 快照窗口随快照删除 | FK CASCADE |
| 审计不可变 | 仅 INSERT 代码路径;无 UPDATE/DELETE 语句(测试断言源码中 audit_log 仅出现于 INSERT) |

## 5. 事务模式示例

投递前登记(原子,queue-safety.md §5 步骤 3):

```sql
BEGIN IMMEDIATE;
-- 重读守门所需的持久状态(防止 CLI 并发改配置)
SELECT ... FROM failures WHERE id = ?;            -- 仍 open
SELECT ... FROM deliveries WHERE failure_id = ?;  -- 无既有行
INSERT INTO deliveries(failure_id, attempt_no, argv, started_at, outcome)
    VALUES (?, 1, ?, ?, 'attempting');
UPDATE failures SET status='delivering' WHERE id=?;
COMMIT;
-- 事务提交成功后才 spawn 子进程
```

## 6. 迁移框架

- 目录 `src/codex_autopilot/store/migrations/`:`NNNN_描述.sql`(NNNN 从 0001 起,即上文 v1 全量;`schema_migrations` 表本身由 runner 在应用任何迁移前 bootstrap 创建,不在迁移文件内)。
- 启动/CLI 打开数据库时:创建 `schema_migrations` → 依序执行未应用迁移(每个迁移一个 IMMEDIATE 事务)→ 记录版本。
- 禁止修改已发布迁移;变更 = 新增迁移。降级不支持(文档说明,回滚靠备份)。
- 迁移执行时 `PRAGMA user_version` 与表内版本在**同一事务**内双写,防半迁移状态;打开数据库时校验两者一致,不一致即拒绝打开(受限,交人工处理)。

## 7. 崩溃恢复(启动清扫)

守护进程启动时按序执行,任何一步失败都进入受限模式(只读命令可用,投递禁用,doctor 报错):

1. `PRAGMA integrity_check` ≠ ok → 受限模式,提示从备份恢复(S-21)。
2. **未决投递清扫**:`outcome='attempting'` 的 deliveries 一律改写为 `unknown` 并对应对话 failure → `needs_human`,附审计说明("进程中断,投递结果未知,不自动重试")。这是 FR-08-A7 的落地点:**宁可让用户确认一次,绝不冒重复投递风险**。
3. 孤儿计划清扫:failure 已终态但 plan 仍 waiting → plan 置 `expired`。
4. 状态自洽检查:session_state 指向的 plan/failure 存在且状态一致;不一致 → 该会话 NEEDS_HUMAN(而非猜测修复)。
5. WAL 检查点(`PRAGMA wal_checkpoint(TRUNCATE)`),记录启动审计。

kill -9 安全性论证:SQLite WAL 崩溃后自动回滚未完成事务;已提交事务(含 `attempting` 登记)必然可见,由步骤 2 收敛。掉电安全性由 `synchronous=FULL` 保证(§2.1):`attempting` 在 spawn 前已 fsync 持久,掉电后登记仍在 → 清扫转 unknown → 人工确认,不会重复投递。任何时点掉电最多导致:多一条 `unknown` 投递(人工确认),**不可能**导致同一失败两次投递——第二次插入会被 UNIQUE 或守门重读拦截。

## 8. 数据保留与体积控制

| 数据 | 保留策略(默认,可配) |
| --- | --- |
| quota_snapshots + windows | 保留 30 天;同账户 15 分钟内同源快照去重不入库 |
| quota_snapshots.raw | 截断至 64 KiB |
| state_history / audit_log | 保留 180 天(审计建议外部备份) |
| notification_log | 保留 90 天 |
| failures/deliveries/confirmations | 与会话同生命周期(软删除后随 purge 清理) |

清理由守护进程每日一次低频执行,删除走批量事务。

## 9. 备份与恢复

- 在线备份:守护进程每日 `VACUUM INTO '$state_dir/backup-YYYYMMDD.db'`(原子、免停服),保留最近 7 份;`codex-autopilot doctor --backup` 可手动触发。
- 文档化等价命令:`sqlite3 state.db ".backup 'backup.db'"`。
- 恢复:停守护进程 → 替换 `state.db`(删除 `-wal`/`-shm`)→ 启动 → 启动清扫自动校验。恢复后首次 tick 全量重评估(状态机以数据库为准,不信任内存)。
- 配置(TOML)与数据库分开备份;数据库不含凭据(NFR-09),SMTP 凭据在配置文件中,备份指引注明脱敏。

## 10. 关键设计决策

| # | 决策 | 理由 | 被否决的替代 |
| --- | --- | --- | --- |
| D-PER-1 | SQLite WAL + 无 ORM + 手写迁移 | 单文件、可审计、免运维;见 §1 | ORM / Postgres / JSON 文件 |
| D-PER-2 | 投递前 `attempting` 先落库再 spawn | 崩溃任何时点都能识别"可能已投递"并收敛为 unknown | 先 spawn 后记录(崩溃窗口内无法察觉已投递) |
| D-PER-3 | `attempting` → `unknown` → needs_human,永不自动重试 | queue 非幂等语义下,重复消息=污染会话,代价远大于人工确认一次 | 自动重投(可能双投) |
| D-PER-4 | 审计 append-only + 仅 INSERT | 可信回溯(NFR-06) | 可编辑日志表 |
| D-PER-5 | daemon flock 单实例 + tick 短暂持锁 | 防双调度器竞争投递(S-17),实现简单 | 仅靠数据库锁(粒度不足,评估期间仍可并发) |
| D-PER-6 | 时间统一 Unix 秒 UTC | 时区零歧义;展示层本地化(NFR-04) | 本地时间字符串(排序/比较地狱) |
| D-PER-7 | `synchronous=FULL`(Phase 2 复审,由 NORMAL 调整) | NORMAL 下 attempting 登记可能在 spawn 后丢失 → 双投;门控状态丢写 → 违背用户暂停意图。论证见 §2.1 | NORMAL(省 fsync,但引入双投风险,收益每 tick 几毫秒) |
