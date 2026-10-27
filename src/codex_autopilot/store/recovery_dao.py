"""恢复域 DAO:failures / recovery_plans / session_state / deliveries(P4)。

约定与 dao.py 相同:读方法直接执行;写方法单语句或显式事务;状态迁移的
多语句原子性由调用方(状态机)包 `Store.transaction()` 保证。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from codex_autopilot.store import Store
from codex_autopilot.store.errors import ConstraintViolationError

# failures.status 取值(persistence §3)
FAILURE_OPEN = "open"
FAILURE_PLANNED = "planned"
FAILURE_DELIVERING = "delivering"
FAILURE_DELIVERED = "delivered"
FAILURE_CONFIRMED = "confirmed"
FAILURE_NEEDS_HUMAN = "needs_human"
FAILURE_SUPERSEDED = "superseded"
FAILURE_CANCELLED = "cancelled"
FAILURE_EXPIRED = "expired"

# recovery_plans.status / strategy
PLAN_WAITING = "waiting"
PLAN_CANCELLED = "cancelled"
PLAN_DELIVERED = "delivered"
PLAN_EXPIRED = "expired"

STRATEGY_WINDOW_RESET = "window_reset"
STRATEGY_MANUAL_ACK = "manual_ack"


@dataclass(frozen=True)
class NewFailure:
    """failures 一行(判定证据见 recovery-state-machine §4.4)。"""

    session_uuid: str
    turn_id: str
    kind: str
    detected_at: int
    evidence: dict[str, Any]  # JSON:判定事件摘要 + 当时窗口/快照引用
    blocking_window_keys: tuple[str, ...] = ()
    status: str = FAILURE_OPEN


class FailureDao:
    def __init__(self, store: Store):
        self._store = store

    def insert(self, record: NewFailure) -> tuple[sqlite3.Row, bool]:
        """插入失败记录;同 (session, turn) 已存在(任意状态)返回 (既有行, False)。

        UNIQUE(session_uuid, turn_id, detected_at) 只挡同刻重复;跨重启重读时
        detected_at 变化,故以 turn 维度显式去重(recovery §4.4 幂等规则)。
        """

        existing = self._store.query_one(
            "SELECT * FROM failures WHERE session_uuid = ? AND turn_id = ?",
            (record.session_uuid, record.turn_id),
        )
        if existing is not None:
            return existing, False
        try:
            self._store.execute(
                "INSERT INTO failures(session_uuid, turn_id, kind, detected_at, evidence,"
                " blocking_window_keys, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record.session_uuid,
                    record.turn_id,
                    record.kind,
                    record.detected_at,
                    json.dumps(record.evidence, ensure_ascii=False, default=str),
                    json.dumps(list(record.blocking_window_keys)),
                    record.status,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConstraintViolationError(str(exc), key="integrity_violation") from exc
        row = self._store.query_one(
            "SELECT * FROM failures WHERE session_uuid = ? AND turn_id = ?",
            (record.session_uuid, record.turn_id),
        )
        assert row is not None  # 刚写入
        return row, True

    def get(self, failure_id: int) -> sqlite3.Row | None:
        return self._store.query_one("SELECT * FROM failures WHERE id = ?", (failure_id,))

    def open_for_session(self, session_uuid: str) -> sqlite3.Row | None:
        """该会话当前 open/planned 的失败(至多一个;recovery §1.4)。"""

        return self._store.query_one(
            "SELECT * FROM failures WHERE session_uuid = ? AND status IN ('open', 'planned')"
            " ORDER BY id DESC LIMIT 1",
            (session_uuid,),
        )

    def set_status(
        self,
        failure_id: int,
        status: str,
        *,
        now: int,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """更新状态;终态同时落 closed_at。可在调用方事务内执行(conn 传入)。"""

        terminal = status in (
            FAILURE_CONFIRMED,
            FAILURE_NEEDS_HUMAN,
            FAILURE_SUPERSEDED,
            FAILURE_CANCELLED,
            FAILURE_EXPIRED,
        )
        sql = "UPDATE failures SET status = ?"
        params: list[Any] = [status]
        if terminal:
            sql += ", closed_at = ?"
            params.append(now)
        sql += " WHERE id = ?"
        params.append(failure_id)
        if conn is not None:
            conn.execute(sql, tuple(params))
        else:
            self._store.execute(sql, tuple(params))

    def waiting_by_account(self, account_id: int) -> list[sqlite3.Row]:
        """该账户下有 waiting 计划的失败(密集刷新判定,quota-monitoring §6)。"""

        return self._store.query(
            "SELECT f.*, p.id AS plan_id, p.not_before AS plan_not_before"
            " FROM failures f"
            " JOIN recovery_plans p ON p.failure_id = f.id AND p.status = 'waiting'"
            " JOIN sessions s ON s.uuid = f.session_uuid"
            " WHERE s.account_id = ? AND s.deleted_at IS NULL AND s.enabled = 1"
            " ORDER BY f.id",
            (account_id,),
        )


class RecoveryPlanDao:
    def __init__(self, store: Store):
        self._store = store

    def insert(
        self,
        failure_id: int,
        *,
        strategy: str,
        not_before: int,
        basis: dict[str, Any],
        now: int,
    ) -> sqlite3.Row:
        try:
            self._store.execute(
                "INSERT INTO recovery_plans(failure_id, strategy, not_before, basis, status,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, 'waiting', ?, ?)",
                (
                    failure_id,
                    strategy,
                    not_before,
                    json.dumps(basis, ensure_ascii=False, default=str),
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConstraintViolationError(str(exc), key="integrity_violation") from exc
        row = self._store.query_one(
            "SELECT * FROM recovery_plans WHERE failure_id = ?", (failure_id,)
        )
        assert row is not None
        return row

    def get(self, plan_id: int) -> sqlite3.Row | None:
        return self._store.query_one("SELECT * FROM recovery_plans WHERE id = ?", (plan_id,))

    def for_failure(self, failure_id: int) -> sqlite3.Row | None:
        return self._store.query_one(
            "SELECT * FROM recovery_plans WHERE failure_id = ?", (failure_id,)
        )

    def waiting_for_session(self, session_uuid: str) -> sqlite3.Row | None:
        return self._store.query_one(
            "SELECT p.* FROM recovery_plans p JOIN failures f ON f.id = p.failure_id"
            " WHERE f.session_uuid = ? AND p.status = 'waiting' ORDER BY p.id DESC LIMIT 1",
            (session_uuid,),
        )

    def set_status(
        self,
        plan_id: int,
        status: str,
        *,
        now: int,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        sql = "UPDATE recovery_plans SET status = ?, updated_at = ? WHERE id = ?"
        params: tuple[Any] = (status, now, plan_id)
        if conn is not None:
            conn.execute(sql, params)
        else:
            self._store.execute(sql, params)


@dataclass(frozen=True)
class StateChange:
    """一次状态迁移的落库载荷(recovery-state-machine §7)。"""

    session_uuid: str
    to_state: str
    reason: str  # 稳定机器码(§7 表)
    evidence: dict[str, Any]
    from_state: str | None = None
    detail: dict[str, Any] | None = None  # 新的 session_state.detail(None=保留)


class SessionStateDao:
    def __init__(self, store: Store):
        self._store = store

    def get(self, session_uuid: str) -> sqlite3.Row | None:
        return self._store.query_one(
            "SELECT * FROM session_state WHERE session_uuid = ?", (session_uuid,)
        )

    def state_of(self, session_uuid: str) -> str | None:
        row = self.get(session_uuid)
        return row["state"] if row is not None else None

    def apply(self, change: StateChange, *, now: int, conn: sqlite3.Connection) -> None:
        """在调用方事务内:写迁移历史 + upsert 当前状态。原子性由事务保证。"""

        conn.execute(
            "INSERT INTO state_history(session_uuid, from_state, to_state, reason, evidence, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                change.session_uuid,
                change.from_state,
                change.to_state,
                change.reason,
                json.dumps(change.evidence, ensure_ascii=False, default=str),
                now,
            ),
        )
        if change.detail is None:
            conn.execute(
                "INSERT INTO session_state(session_uuid, state, since, detail)"
                " VALUES (?, ?, ?, NULL)"
                " ON CONFLICT(session_uuid) DO UPDATE SET state = excluded.state,"
                " since = excluded.since",
                (change.session_uuid, change.to_state, now),
            )
        else:
            conn.execute(
                "INSERT INTO session_state(session_uuid, state, since, detail)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(session_uuid) DO UPDATE SET state = excluded.state,"
                " since = excluded.since, detail = excluded.detail",
                (
                    change.session_uuid,
                    change.to_state,
                    now,
                    json.dumps(change.detail, ensure_ascii=False, default=str),
                ),
            )

    def history(self, session_uuid: str | None = None, *, limit: int = 50) -> list[sqlite3.Row]:
        if session_uuid is None:
            return self._store.query(
                "SELECT * FROM state_history ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
            )
        return self._store.query(
            "SELECT * FROM state_history WHERE session_uuid = ? ORDER BY ts DESC, id DESC LIMIT ?",
            (session_uuid, limit),
        )


class DeliveryDao:
    """投递记录读取 + 启动清扫(G4/G12 数据源;投递写入属 Phase 5)。"""

    def __init__(self, store: Store):
        self._store = store

    def for_failure(self, failure_id: int) -> list[sqlite3.Row]:
        return self._store.query(
            "SELECT * FROM deliveries WHERE failure_id = ? ORDER BY id", (failure_id,)
        )

    def last_for_session(self, session_uuid: str) -> sqlite3.Row | None:
        return self._store.query_one(
            "SELECT d.* FROM deliveries d JOIN failures f ON f.id = d.failure_id"
            " WHERE f.session_uuid = ? ORDER BY d.started_at DESC, d.id DESC LIMIT 1",
            (session_uuid,),
        )

    def attempting(self) -> list[sqlite3.Row]:
        return self._store.query("SELECT * FROM deliveries WHERE outcome = 'attempting'")

    def sweep_attempting(self, *, now: int, actor: str = "recovery:sweep") -> list[int]:
        """启动清扫:persistence §7 步骤 2 —— attempting → unknown + failure → needs_human。

        返回收敛的 delivery id 列表(供审计)。FR-08-A7:宁人工确认,不冒双投风险。
        """

        resolved: list[int] = []
        for row in self.attempting():
            failure = self._store.query_one(
                "SELECT session_uuid FROM failures WHERE id = ?", (row["failure_id"],)
            )
            session_uuid = failure["session_uuid"] if failure is not None else None
            with self._store.transaction() as conn:
                conn.execute(
                    "UPDATE deliveries SET outcome = 'unknown', ended_at = ?,"
                    " receipt = ? WHERE id = ? AND outcome = 'attempting'",
                    (
                        now,
                        json.dumps({"classification": "swept_unknown"}),
                        row["id"],
                    ),
                )
                conn.execute(
                    "UPDATE failures SET status = 'needs_human', closed_at = ?"
                    " WHERE id = ? AND status IN ('open', 'planned', 'delivering')",
                    (now, row["failure_id"]),
                )
                if session_uuid is not None:
                    conn.execute(
                        "INSERT INTO session_state(session_uuid, state, since, detail)"
                        " VALUES (?, 'NEEDS_HUMAN', ?, ?)"
                        " ON CONFLICT(session_uuid) DO UPDATE SET state = 'NEEDS_HUMAN',"
                        " since = excluded.since",
                        (
                            session_uuid,
                            now,
                            json.dumps({"delivery_id": row["id"], "swept": True}),
                        ),
                    )
                    conn.execute(
                        "INSERT INTO state_history(session_uuid, from_state, to_state, reason,"
                        " evidence, ts) VALUES (?, NULL, 'NEEDS_HUMAN', 'swept_attempting',"
                        " ?, ?)",
                        (
                            session_uuid,
                            json.dumps({"delivery_id": row["id"], "actor": actor}),
                            now,
                        ),
                    )
            resolved.append(int(row["id"]))
        return resolved
