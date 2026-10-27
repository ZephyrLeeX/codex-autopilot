"""基础 DAO:accounts / sessions / runtime_flags(P2-T2)。

约定:
- 读方法直接执行(自动提交的 SELECT);
- 写方法只发单条语句,调用方决定是否包在 `Store.transaction()` 内以获得
  多语句原子性(例如 CLI `add` = 账户 + 会话 + 审计同一事务);
- 唯一约束/外键冲突统一映射为 ConstraintViolationError(key 稳定,CLI 用于
  JSON 错误码)。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from codex_autopilot.store import Store
from codex_autopilot.store.errors import ConstraintViolationError
from codex_autopilot.util.identifiers import canonical_uuid


@dataclass(frozen=True)
class NewSession:
    """`add` 的写入载荷(校验后的最终值)。"""

    uuid: str
    name: str
    project_path: str
    account_id: int
    description: str | None = None
    auto_resume: bool = False
    tmux_session: str | None = None
    resume_template: str | None = None
    notify_policy: dict[str, Any] | None = None
    check_interval: int | None = None


_CONSTRAINT_KEYS = {
    "sessions.uuid": "duplicate_uuid",
    "sessions.name": "duplicate_name",
    "accounts.codex_home": "duplicate_account",
}


def _map_integrity_error(exc: sqlite3.IntegrityError) -> ConstraintViolationError:
    message = str(exc)
    for needle, key in _CONSTRAINT_KEYS.items():
        if needle in message:
            return ConstraintViolationError(f"constraint violated: {needle} ({message})", key=key)
    if "FOREIGN KEY constraint failed" in message:
        return ConstraintViolationError(message, key="fk_violation")
    if "UNIQUE constraint failed" in message:
        return ConstraintViolationError(message, key="unique_violation")
    return ConstraintViolationError(message, key="integrity_violation")


class AccountDao:
    def __init__(self, store: Store):
        self._store = store

    def get_or_create(self, codex_home: str, *, label: str | None, now: int) -> sqlite3.Row:
        """按规范化 codex_home 查找;不存在则创建。竞态下重复插入由 UNIQUE 兜底。"""

        row = self._store.query_one("SELECT * FROM accounts WHERE codex_home = ?", (codex_home,))
        if row is not None:
            return row
        try:
            self._store.execute(
                "INSERT INTO accounts(codex_home, label, created_at) VALUES (?, ?, ?)",
                (codex_home, label, now),
            )
        except sqlite3.IntegrityError as exc:
            if "accounts.codex_home" in str(exc):
                row = self._store.query_one(
                    "SELECT * FROM accounts WHERE codex_home = ?", (codex_home,)
                )
                if row is not None:
                    return row
            raise _map_integrity_error(exc) from exc
        row = self._store.query_one("SELECT * FROM accounts WHERE codex_home = ?", (codex_home,))
        assert row is not None
        return row

    def get_by_id(self, account_id: int) -> sqlite3.Row | None:
        return self._store.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))


class SessionDao:
    def __init__(self, store: Store):
        self._store = store

    def insert(self, record: NewSession, *, now: int) -> sqlite3.Row:
        canonical = canonical_uuid(record.uuid)  # 防御:入库前再收紧一次格式
        try:
            self._store.execute(
                "INSERT INTO sessions(uuid, name, description, project_path, account_id,"
                " enabled, auto_resume, tmux_session, resume_template, notify_policy,"
                " check_interval, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
                (
                    canonical,
                    record.name,
                    record.description,
                    record.project_path,
                    record.account_id,
                    1 if record.auto_resume else 0,
                    record.tmux_session,
                    record.resume_template,
                    (
                        json.dumps(record.notify_policy, ensure_ascii=False)
                        if record.notify_policy is not None
                        else None
                    ),
                    record.check_interval,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise _map_integrity_error(exc) from exc
        return self.get_by_uuid(canonical)  # type: ignore[return-value]

    def get_by_uuid(self, uuid: str) -> sqlite3.Row | None:
        return self._store.query_one(
            "SELECT * FROM sessions WHERE uuid = ?", (canonical_uuid(uuid),)
        )

    def get_by_name(self, name: str) -> sqlite3.Row | None:
        return self._store.query_one("SELECT * FROM sessions WHERE name = ?", (name,))

    def resolve(self, name_or_uuid: str) -> sqlite3.Row | None:
        """按名称或完整 UUID 精确解析;绝不做前缀模糊匹配(D-CLI-2/G5)。"""

        row = self.get_by_name(name_or_uuid)
        if row is not None:
            return row
        try:
            return self.get_by_uuid(name_or_uuid)
        except ValueError:
            return None

    def list_active(self) -> list[sqlite3.Row]:
        """未删除会话 + 账户信息 + 当前状态缓存 + 最近状态事件时间。"""

        return self._store.query(
            "SELECT s.*, a.codex_home AS account_codex_home, a.label AS account_label,"
            " st.state AS state, st.since AS state_since,"
            " (SELECT MAX(ts) FROM state_history h WHERE h.session_uuid = s.uuid)"
            "   AS last_event_at"
            " FROM sessions s"
            " JOIN accounts a ON a.id = s.account_id"
            " LEFT JOIN session_state st ON st.session_uuid = s.uuid"
            " WHERE s.deleted_at IS NULL"
            " ORDER BY s.name"
        )

    def soft_delete(self, uuid: str, *, now: int) -> int:
        cursor = self._store.execute(
            "UPDATE sessions SET deleted_at = ?, updated_at = ?"
            " WHERE uuid = ? AND deleted_at IS NULL",
            (now, now, canonical_uuid(uuid)),
        )
        return cursor.rowcount

    def purge(self, uuid: str) -> None:
        """物理删除会话及其全部从属记录(FK 顺序);审计保留(persistence §4)。"""

        canonical = canonical_uuid(uuid)
        self._store.execute(
            "DELETE FROM confirmations WHERE delivery_id IN ("
            " SELECT d.id FROM deliveries d JOIN failures f ON f.id = d.failure_id"
            " WHERE f.session_uuid = ?)",
            (canonical,),
        )
        self._store.execute(
            "DELETE FROM deliveries WHERE failure_id IN ("
            " SELECT id FROM failures WHERE session_uuid = ?)",
            (canonical,),
        )
        self._store.execute(
            "DELETE FROM recovery_plans WHERE failure_id IN ("
            " SELECT id FROM failures WHERE session_uuid = ?)",
            (canonical,),
        )
        self._store.execute("DELETE FROM failures WHERE session_uuid = ?", (canonical,))
        self._store.execute("DELETE FROM state_history WHERE session_uuid = ?", (canonical,))
        self._store.execute("DELETE FROM session_state WHERE session_uuid = ?", (canonical,))
        self._store.execute("DELETE FROM sessions WHERE uuid = ?", (canonical,))


@dataclass(frozen=True)
class NewQuotaWindow:
    """quota_windows 一行(字段与 persistence §3 对齐;领域模型在 quota/model)。"""

    bucket_key: str
    window_kind: str
    kind_inferred: bool = False
    used_percent: float | None = None
    resets_at: int | None = None
    window_minutes: int | None = None
    is_exhausted: bool = False


@dataclass(frozen=True)
class NewQuotaSnapshot:
    """额度快照写入载荷(source/captured_at/ok 语义见 quota-monitoring §2/§4)。

    判定相关标志(ordinary/spend_control/reached_type/reset_credits)自 0002
    迁移起结构化入库,供跨快照比较(G6/E1/E3);此前仅存 raw(会截断)。
    """

    source: str  # 'appserver' | 'jsonl'
    captured_at: int
    ok: bool
    error: str | None = None
    raw: str | None = None  # 原始 JSON;调用方负责截断(persistence §8 上限 64KiB)
    parser_version: str = ""
    windows: tuple[NewQuotaWindow, ...] = ()
    ordinary_usage_allowed: bool | None = None
    spend_control_reached: bool = False
    rate_limit_reached_type: str | None = None
    reset_credits_json: str | None = None  # ResetCredits 的 JSON 序列化


RAW_SNAPSHOT_MAX_BYTES = 64 * 1024  # persistence §8:raw 截断上限


class QuotaSnapshotDao:
    """额度快照与窗口明细(persistence §3;去重规则 §8)。"""

    def __init__(self, store: Store):
        self._store = store

    def insert(
        self, account_id: int, snapshot: NewQuotaSnapshot, *, ingested_at: int
    ) -> int | None:
        """单事务写入快照 + 窗口明细;去重规则:同账户同源同 captured_at 且
        **raw 相同**才跳过(persistence §8 控体积)。

        raw 参与比较是正确性要求:同一秒内的两次查询(tick 与手动 check)
        数据可能不同,只按时间戳去重会静默丢弃新数据,评估用到过期快照。
        """

        raw = snapshot.raw
        if raw is not None and len(raw.encode("utf-8")) > RAW_SNAPSHOT_MAX_BYTES:
            raw = raw.encode("utf-8")[:RAW_SNAPSHOT_MAX_BYTES].decode("utf-8", errors="ignore")
        row = self._store.query_one(
            "SELECT id, raw FROM quota_snapshots"
            " WHERE account_id = ? AND source = ? AND captured_at = ?",
            (account_id, snapshot.source, snapshot.captured_at),
        )
        if row is not None and row["raw"] == raw:
            return None
        with self._store.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO quota_snapshots(account_id, source, captured_at, ingested_at, ok,"
                " error, raw, parser_version, ordinary_usage_allowed, spend_control_reached,"
                " rate_limit_reached_type, reset_credits)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    account_id,
                    snapshot.source,
                    snapshot.captured_at,
                    ingested_at,
                    1 if snapshot.ok else 0,
                    snapshot.error,
                    raw,
                    snapshot.parser_version,
                    (
                        None
                        if snapshot.ordinary_usage_allowed is None
                        else 1
                        if snapshot.ordinary_usage_allowed
                        else 0
                    ),
                    1 if snapshot.spend_control_reached else 0,
                    snapshot.rate_limit_reached_type,
                    snapshot.reset_credits_json,
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            conn.executemany(
                "INSERT INTO quota_windows(snapshot_id, bucket_key, window_kind, kind_inferred,"
                " used_percent, resets_at, window_minutes, is_exhausted)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        snapshot_id,
                        w.bucket_key,
                        w.window_kind,
                        1 if w.kind_inferred else 0,
                        w.used_percent,
                        w.resets_at,
                        w.window_minutes,
                        1 if w.is_exhausted else 0,
                    )
                    for w in snapshot.windows
                ],
            )
        return snapshot_id

    def _snapshot_row_with_windows(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["ok"] = bool(data["ok"])
        data["ordinary_usage_allowed"] = (
            None
            if data.get("ordinary_usage_allowed") is None
            else bool(data["ordinary_usage_allowed"])
        )
        data["spend_control_reached"] = bool(data.get("spend_control_reached", 0))
        data["windows"] = [
            {
                "bucket_key": w["bucket_key"],
                "window_kind": w["window_kind"],
                "kind_inferred": bool(w["kind_inferred"]),
                "used_percent": w["used_percent"],
                "resets_at": w["resets_at"],
                "window_minutes": w["window_minutes"],
                "is_exhausted": bool(w["is_exhausted"]),
            }
            for w in self._store.query(
                "SELECT * FROM quota_windows WHERE snapshot_id = ? ORDER BY id", (row["id"],)
            )
        ]
        return data

    def latest_ok(self, account_id: int, *, source: str | None = None) -> dict[str, Any] | None:
        """最新 ok=1 快照(含窗口明细);无则 None。"""

        sql = "SELECT * FROM quota_snapshots WHERE account_id = ? AND ok = 1"
        params: list[Any] = [account_id]
        if source is not None:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY captured_at DESC, id DESC LIMIT 1"
        row = self._store.query_one(sql, tuple(params))
        return self._snapshot_row_with_windows(row) if row is not None else None

    def latest_ok_before(self, account_id: int, ts: int) -> dict[str, Any] | None:
        """captured_at ≤ ts 的最新 ok=1 快照(失败时刻基线;提前恢复信号比较用)。"""

        row = self._store.query_one(
            "SELECT * FROM quota_snapshots WHERE account_id = ? AND ok = 1"
            " AND captured_at <= ? ORDER BY captured_at DESC, id DESC LIMIT 1",
            (account_id, ts),
        )
        return self._snapshot_row_with_windows(row) if row is not None else None

    def ok_snapshots_since(
        self, account_id: int, since_ts: int, *, source: str = "appserver"
    ) -> list[dict[str, Any]]:
        """captured_at > since_ts 的 ok=1 快照(升序;G6 滚动证据候选,含窗口明细)。"""

        rows = self._store.query(
            "SELECT * FROM quota_snapshots WHERE account_id = ? AND ok = 1 AND source = ?"
            " AND captured_at > ? ORDER BY captured_at ASC, id ASC",
            (account_id, source, since_ts),
        )
        return [self._snapshot_row_with_windows(row) for row in rows]

    def snapshot_by_id(self, snapshot_id: int) -> dict[str, Any] | None:
        row = self._store.query_one("SELECT * FROM quota_snapshots WHERE id = ?", (snapshot_id,))
        return self._snapshot_row_with_windows(row) if row is not None else None

    def latest_error(self, account_id: int, *, source: str = "appserver") -> dict[str, Any] | None:
        """最近一次 ok=0 错误快照(展示降级提示用)。"""

        row = self._store.query_one(
            "SELECT * FROM quota_snapshots WHERE account_id = ? AND source = ? AND ok = 0"
            " ORDER BY captured_at DESC, id DESC LIMIT 1",
            (account_id, source),
        )
        return dict(row) if row is not None else None


class RuntimeFlagsDao:
    """运行期标志(global_pause 等;值以 JSON 存储,persistence §3)。"""

    def __init__(self, store: Store):
        self._store = store

    def get(self, key: str) -> Any | None:
        row = self._store.query_one("SELECT value FROM runtime_flags WHERE key = ?", (key,))
        if row is None:
            return None
        return json.loads(row["value"])

    def set(self, key: str, value: Any, *, now: int) -> None:
        self._store.execute(
            "INSERT INTO runtime_flags(key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), now),
        )
