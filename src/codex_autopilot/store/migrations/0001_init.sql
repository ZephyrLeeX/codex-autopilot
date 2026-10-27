-- 0001: 初始模式(persistence.md §3 Schema v1)
-- 规则:迁移只增不改;时间戳统一 Unix 秒(UTC,INTEGER);JSON 字段存 TEXT。
-- schema_migrations 表由迁移 runner 在应用任何迁移前 bootstrap 创建(persistence §6),
-- 不属于迁移内容本身。

CREATE TABLE runtime_flags (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE accounts (
    id          INTEGER PRIMARY KEY,
    codex_home  TEXT NOT NULL UNIQUE,
    label       TEXT,
    created_at  INTEGER NOT NULL
);

CREATE TABLE sessions (
    uuid            TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    project_path    TEXT NOT NULL,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    enabled         INTEGER NOT NULL DEFAULT 1,
    auto_resume     INTEGER NOT NULL DEFAULT 0,
    tmux_session    TEXT,
    resume_template TEXT,
    notify_policy   TEXT,
    check_interval  INTEGER,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    deleted_at      INTEGER
);
CREATE INDEX idx_sessions_account ON sessions(account_id) WHERE deleted_at IS NULL;

CREATE TABLE session_state (
    session_uuid TEXT PRIMARY KEY REFERENCES sessions(uuid),
    state        TEXT NOT NULL,
    since        INTEGER NOT NULL,
    detail       TEXT
);

CREATE TABLE state_history (
    id          INTEGER PRIMARY KEY,
    session_uuid TEXT NOT NULL REFERENCES sessions(uuid),
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    reason      TEXT NOT NULL,
    evidence    TEXT NOT NULL,
    ts          INTEGER NOT NULL
);
CREATE INDEX idx_state_history_session ON state_history(session_uuid, ts);

CREATE TABLE quota_snapshots (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    source      TEXT NOT NULL,
    captured_at INTEGER NOT NULL,
    ingested_at INTEGER NOT NULL,
    ok          INTEGER NOT NULL,
    error       TEXT,
    raw         TEXT,
    parser_version TEXT NOT NULL
);
CREATE INDEX idx_snapshots_account ON quota_snapshots(account_id, captured_at DESC);

CREATE TABLE quota_windows (
    id              INTEGER PRIMARY KEY,
    snapshot_id     INTEGER NOT NULL REFERENCES quota_snapshots(id) ON DELETE CASCADE,
    bucket_key      TEXT NOT NULL,
    window_kind     TEXT NOT NULL,
    kind_inferred   INTEGER NOT NULL DEFAULT 0,
    used_percent    REAL,
    resets_at       INTEGER,
    window_minutes  INTEGER,
    is_exhausted    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE failures (
    id              INTEGER PRIMARY KEY,
    session_uuid    TEXT NOT NULL REFERENCES sessions(uuid),
    turn_id         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    detected_at     INTEGER NOT NULL,
    evidence        TEXT NOT NULL,
    blocking_window_keys TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    closed_at       INTEGER,
    UNIQUE(session_uuid, turn_id, detected_at)
);

CREATE TABLE recovery_plans (
    id          INTEGER PRIMARY KEY,
    failure_id  INTEGER NOT NULL UNIQUE REFERENCES failures(id),
    strategy    TEXT NOT NULL,
    not_before  INTEGER NOT NULL,
    basis       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'waiting',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE deliveries (
    id          INTEGER PRIMARY KEY,
    failure_id  INTEGER NOT NULL REFERENCES failures(id),
    attempt_no  INTEGER NOT NULL,
    argv        TEXT NOT NULL,
    started_at  INTEGER NOT NULL,
    ended_at    INTEGER,
    outcome     TEXT NOT NULL DEFAULT 'attempting',
    receipt     TEXT,
    UNIQUE(failure_id, attempt_no)
);
CREATE INDEX idx_delivers_failure ON deliveries(failure_id);

CREATE TABLE confirmations (
    id          INTEGER PRIMARY KEY,
    delivery_id INTEGER NOT NULL UNIQUE REFERENCES deliveries(id),
    method      TEXT NOT NULL,
    confirmed_at INTEGER,
    evidence    TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE notification_log (
    id          INTEGER PRIMARY KEY,
    dedup_key   TEXT NOT NULL,
    channel     TEXT NOT NULL,
    event       TEXT NOT NULL,
    session_uuid TEXT,
    status      TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_attempt_at INTEGER,
    sent_at     INTEGER,
    payload     TEXT,
    UNIQUE(dedup_key, channel)
);

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL,
    actor       TEXT NOT NULL,
    category    TEXT NOT NULL,
    level       TEXT NOT NULL,
    session_uuid TEXT,
    summary     TEXT NOT NULL,
    data        TEXT
);
CREATE INDEX idx_audit_ts ON audit_log(ts);
CREATE INDEX idx_audit_session ON audit_log(session_uuid, ts);
