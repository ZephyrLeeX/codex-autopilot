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
