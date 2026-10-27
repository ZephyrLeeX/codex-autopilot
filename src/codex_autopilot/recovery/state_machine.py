"""恢复状态机:全部迁移 + 持久化 + 覆盖层 + fail-closed(P4-T3)。

设计(recovery-state-machine.md):
- 状态机是唯一真相源:一切"现在该做什么"来自持久化状态 + 迁移规则;
  每次迁移原子落库(session_state + state_history 同一 IMMEDIATE 事务),
  并带稳定 reason 码与 evidence JSON;
- PAUSED 是覆盖层而非存储状态:session_state.state 存主状态,`enabled=0`
  冻结一切自动迁移(§3 注);展示层用 `effective_state()` 叠加 PAUSED;
- MISSING 为存储状态(带 missing_from_state 的 detail,恢复时回原状态);
- fail-closed:任何"无法证实"的迁移一律拒绝或转 NEEDS_HUMAN,绝不乐观
  前进;非法迁移抛 InvalidTransitionError(调用方审计 + 跳过)。

Phase 4 边界:投递相关迁移(DELIVERING/DELIVERED_UNCONFIRMED/RECOVERED)
已实现但无调用方 —— 投递执行器属 Phase 5(P5-T2 接入)。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from codex_autopilot.recovery.model import NEEDS_HUMAN_KINDS, DetectedFailure
from codex_autopilot.store import Store
from codex_autopilot.store.recovery_dao import (
    FAILURE_NEEDS_HUMAN,
    FAILURE_OPEN,
    FAILURE_PLANNED,
    FAILURE_SUPERSEDED,
    PLAN_CANCELLED,
    PLAN_DELIVERED,
    PLAN_WAITING,
    FailureDao,
    RecoveryPlanDao,
    SessionStateDao,
    StateChange,
)
from codex_autopilot.util.clock import TimeSource

# 状态集(recovery-state-machine §2)
ACTIVE = "ACTIVE"
FAILURE_DETECTED = "FAILURE_DETECTED"
WAITING_RECOVERY = "WAITING_RECOVERY"
DELIVERING = "DELIVERING"
DELIVERED_UNCONFIRMED = "DELIVERED_UNCONFIRMED"
RECOVERED = "RECOVERED"
SUPERSEDED = "SUPERSEDED"
NEEDS_HUMAN = "NEEDS_HUMAN"
MISSING = "MISSING"

# 终态(对本 failure;随即回 ACTIVE)
_EPHEMERAL_TERMINAL = frozenset({RECOVERED, SUPERSEDED})

# reason 码(§7 表 + Phase 4 补充;稳定机器码,不做文案)
R_FAILURE_DETECTED = "failure_detected"
R_PLAN_ARMED = "plan_armed"
R_GUARDS_PASSED = "guards_passed"
R_QUEUE_ACCEPTED = "queue_accepted"
R_QUEUE_REJECTED = "queue_rejected"
R_QUEUE_UNKNOWN = "queue_unknown"
R_CONFIRM_TIMEOUT = "confirm_timeout"
R_ABORT_NO_QUOTA = "abort_no_quota"
R_UNKNOWN_QUOTA_KIND = "unknown_quota_kind"
R_MISSING_OVER_GRACE = "missing_over_grace"
R_DB_INCONSISTENT = "db_inconsistent"
R_HUMAN_MESSAGE_SEEN = "human_message_seen"
R_NEW_TURN_CONFIRMED = "new_turn_confirmed"
R_MANUAL_ACK = "manual_ack"
R_FAILURE_CLOSED_SUPERSEDED = "failure_closed_superseded"
R_FAILURE_CLOSED_CONFIRMED = "failure_closed_confirmed"
R_ROLLOUT_MISSING = "rollout_missing"
R_ROLLOUT_RESTORED = "rollout_restored"
# Phase 4 补充码(非 §7 表内的直接 NEEDS_HUMAN 路径,文档已登记)
R_TURN_INTERRUPTED = "turn_interrupted"
R_TERMINAL_FAILURE = "terminal_failure"
R_UNKNOWN_ERROR_VARIANT = "unknown_error_variant"


class StateMachineError(Exception):
    """状态机层错误(迁移非法/会话不存在/被暂停冻结)。"""


class InvalidTransitionError(StateMachineError):
    def __init__(self, session_uuid: str, from_state: str | None, to_state: str, reason: str):
        self.session_uuid = session_uuid
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"invalid transition {from_state!r} -> {to_state!r} for {session_uuid}: {reason}"
        )


class SessionPausedError(StateMachineError):
    """会话 enabled=0:一切自动迁移冻结(session-management §4)。"""


def effective_state(state_row: sqlite3.Row | None, *, enabled: bool) -> str:
    """展示用有效状态:PAUSED 覆盖层(session-management §4/D-SM-2)。"""

    if not enabled:
        return "PAUSED"
    if state_row is None:
        return ACTIVE
    return state_row["state"]


class RecoveryStateMachine:
    def __init__(self, store: Store, clock: TimeSource):
        self._store = store
        self._clock = clock
        self._states = SessionStateDao(store)
        self._failures = FailureDao(store)
        self._plans = RecoveryPlanDao(store)

    # -- 基础 -------------------------------------------------------------

    def current(self, session_uuid: str) -> sqlite3.Row | None:
        return self._states.get(session_uuid)

    def state_of(self, session_uuid: str) -> str | None:
        """当前主状态(不含覆盖层;None = 尚无状态行,按 ACTIVE 语义)。"""

        return self._states.state_of(session_uuid)

    def _require_session(self, session_uuid: str) -> sqlite3.Row:
        row = self._store.query_one(
            "SELECT * FROM sessions WHERE uuid = ? AND deleted_at IS NULL", (session_uuid,)
        )
        if row is None:
            raise StateMachineError(f"session not found or deleted: {session_uuid}")
        return row

    def _require_enabled(self, session_uuid: str) -> sqlite3.Row:
        row = self._require_session(session_uuid)
        if not row["enabled"]:
            raise SessionPausedError(f"session {session_uuid} is paused (enabled=0)")
        return row

    def _detail(self, session_uuid: str) -> dict[str, Any]:
        row = self._states.get(session_uuid)
        if row is None or row["detail"] is None:
            return {}
        try:
            data = json.loads(row["detail"])
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _apply(
        self,
        session_uuid: str,
        to_state: str,
        reason: str,
        evidence: dict[str, Any],
        *,
        detail: dict[str, Any] | None = None,
        expect_from: tuple[str, ...] | None = None,
    ) -> None:
        """原子迁移:校验 from → 写 history + upsert state(单一事务)。"""

        now = self._clock.epoch()
        with self._store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM session_state WHERE session_uuid = ?", (session_uuid,)
            ).fetchone()
            from_state = row["state"] if row is not None else None
            if expect_from is not None and from_state not in expect_from:
                raise InvalidTransitionError(
                    session_uuid, from_state, to_state, "unexpected source state"
                )
            self._states.apply(
                StateChange(
                    session_uuid=session_uuid,
                    to_state=to_state,
                    reason=reason,
                    evidence=evidence,
                    from_state=from_state,
                    detail=detail,
                ),
                now=now,
                conn=conn,
            )

    # -- 检测 → FAILURE_DETECTED(§4) -------------------------------------

    def record_failure_for(
        self, session_uuid: str, failure: DetectedFailure
    ) -> tuple[sqlite3.Row, str]:
        """落库失败并迁移:额度类 → FAILURE_DETECTED;终态类直接 NEEDS_HUMAN。

        幂等:同 turn 已有失败记录时返回 (既有行, 'duplicate') 且不迁移。
        返回动作 ∈ {'failure_detected', 'needs_human', 'duplicate'}。
        """

        self._require_enabled(session_uuid)
        now = self._clock.epoch()
        row, created = self._failures.insert(_new_failure_row(session_uuid, failure))
        if not created:
            return row, "duplicate"

        if failure.kind in NEEDS_HUMAN_KINDS:
            reason = {
                "aborted": R_TURN_INTERRUPTED,
                "terminal_error": R_TERMINAL_FAILURE,
                "unknown_error": R_UNKNOWN_ERROR_VARIANT,
            }[failure.kind]
            with self._store.transaction() as conn:
                self._failures.set_status(int(row["id"]), FAILURE_NEEDS_HUMAN, now=now, conn=conn)
            row = self._failures.get(int(row["id"])) or row  # 返回更新后的行
            self._apply(
                session_uuid,
                NEEDS_HUMAN,
                reason,
                {
                    "failure_id": int(row["id"]),
                    "kind": failure.kind,
                    "error_variant": failure.evidence.get("error_variant"),
                },
                expect_from=(ACTIVE, None),
            )
            self._audit_transition(session_uuid, NEEDS_HUMAN, reason, int(row["id"]))
            return row, "needs_human"

        self._apply(
            session_uuid,
            FAILURE_DETECTED,
            R_FAILURE_DETECTED,
            {
                "failure_id": int(row["id"]),
                "kind": failure.kind,
                "turn_id": failure.turn_id,
                "blocking_window_keys": list(failure.blocking_window_keys),
                "error_variant": failure.evidence.get("error_variant"),
            },
            expect_from=(ACTIVE, None),
        )
        self._audit_transition(session_uuid, FAILURE_DETECTED, R_FAILURE_DETECTED, int(row["id"]))
        return row, "failure_detected"

    # -- 计划 -------------------------------------------------------------

    def arm_plan(self, session_uuid: str, failure_id: int, plan_id: int) -> None:
        """FAILURE_DETECTED → WAITING_RECOVERY(计划已建好;失败 → planned)。"""

        self._require_enabled(session_uuid)
        # 先校验来源状态(给出精确错误码;_apply 内会原子复核)
        current = self._states.state_of(session_uuid)
        if current not in (FAILURE_DETECTED, None):
            raise InvalidTransitionError(
                session_uuid, current, WAITING_RECOVERY, "arm_plan requires FAILURE_DETECTED"
            )
        now = self._clock.epoch()
        with self._store.transaction() as conn:
            failure = conn.execute("SELECT * FROM failures WHERE id = ?", (failure_id,)).fetchone()
            if failure is None or failure["status"] != FAILURE_OPEN:
                raise StateMachineError(f"failure {failure_id} not open for planning")
            plan = conn.execute("SELECT * FROM recovery_plans WHERE id = ?", (plan_id,)).fetchone()
            if plan is None or plan["status"] != PLAN_WAITING:
                raise StateMachineError(f"plan {plan_id} not waiting")
            self._failures.set_status(failure_id, FAILURE_PLANNED, now=now, conn=conn)
        self._apply(
            session_uuid,
            WAITING_RECOVERY,
            R_PLAN_ARMED,
            {
                "failure_id": failure_id,
                "plan_id": plan_id,
                "strategy": plan["strategy"],
                "not_before": plan["not_before"],
                "blocking_windows": _plan_basis_windows(plan),
            },
            expect_from=(FAILURE_DETECTED, None),
            detail={"failure_id": failure_id, "plan_id": plan_id},
        )
        self._audit_transition(session_uuid, WAITING_RECOVERY, R_PLAN_ARMED, failure_id)

    def fail_to_human(
        self, session_uuid: str, reason: str, evidence: dict[str, Any] | None = None
    ) -> None:
        """→ NEEDS_HUMAN(fail-closed 汇聚点;任意自动状态可达)。

        同时关闭该会话 open/planned 的失败并取消 waiting 计划。
        """

        self._require_enabled(session_uuid)
        now = self._clock.epoch()
        evidence = dict(evidence or {})
        with self._store.transaction() as conn:
            failure = conn.execute(
                "SELECT * FROM failures WHERE session_uuid = ? AND status IN (?, ?)"
                " ORDER BY id DESC LIMIT 1",
                (session_uuid, FAILURE_OPEN, FAILURE_PLANNED),
            ).fetchone()
            if failure is not None:
                self._failures.set_status(
                    int(failure["id"]), FAILURE_NEEDS_HUMAN, now=now, conn=conn
                )
                evidence["failure_id"] = int(failure["id"])
                plan = conn.execute(
                    "SELECT * FROM recovery_plans WHERE failure_id = ?", (failure["id"],)
                ).fetchone()
                if plan is not None and plan["status"] == PLAN_WAITING:
                    self._plans.set_status(int(plan["id"]), PLAN_CANCELLED, now=now, conn=conn)
                    evidence["cancelled_plan_id"] = int(plan["id"])
        self._apply(session_uuid, NEEDS_HUMAN, reason, evidence)
        self._audit_transition(session_uuid, NEEDS_HUMAN, reason, evidence.get("failure_id"))

    # -- 人工接管 ----------------------------------------------------------

    def supersede(self, session_uuid: str, evidence: dict[str, Any]) -> None:
        """WAITING_RECOVERY/FAILURE_DETECTED → SUPERSEDED → ACTIVE(人工介入)。

        failure → superseded;waiting 计划 → cancelled。两条迁移同一事务。
        """

        self._require_enabled(session_uuid)
        now = self._clock.epoch()
        with self._store.transaction() as conn:
            failure = conn.execute(
                "SELECT * FROM failures WHERE session_uuid = ? AND status IN (?, ?)"
                " ORDER BY id DESC LIMIT 1",
                (session_uuid, FAILURE_OPEN, FAILURE_PLANNED),
            ).fetchone()
            if failure is None:
                raise StateMachineError(f"no open failure to supersede for {session_uuid}")
            failure_id = int(failure["id"])
            self._failures.set_status(failure_id, FAILURE_SUPERSEDED, now=now, conn=conn)
            plan = conn.execute(
                "SELECT * FROM recovery_plans WHERE failure_id = ?", (failure_id,)
            ).fetchone()
            if plan is not None and plan["status"] == PLAN_WAITING:
                self._plans.set_status(int(plan["id"]), PLAN_CANCELLED, now=now, conn=conn)
            row = conn.execute(
                "SELECT * FROM session_state WHERE session_uuid = ?", (session_uuid,)
            ).fetchone()
            from_state = row["state"] if row is not None else None
            if from_state not in (WAITING_RECOVERY, FAILURE_DETECTED, ACTIVE):
                raise InvalidTransitionError(
                    session_uuid, from_state, SUPERSEDED, "no open failure context"
                )
            self._states.apply(
                StateChange(
                    session_uuid=session_uuid,
                    to_state=SUPERSEDED,
                    reason=R_HUMAN_MESSAGE_SEEN,
                    evidence=evidence,
                    from_state=from_state,
                ),
                now=now,
                conn=conn,
            )
            self._states.apply(
                StateChange(
                    session_uuid=session_uuid,
                    to_state=ACTIVE,
                    reason=R_FAILURE_CLOSED_SUPERSEDED,
                    evidence={"failure_id": failure_id},
                    from_state=SUPERSEDED,
                ),
                now=now,
                conn=conn,
            )
        self._audit_transition(session_uuid, SUPERSEDED, R_HUMAN_MESSAGE_SEEN, failure_id)

    def ack_needs_human(self, session_uuid: str, *, operator: str) -> None:
        """NEEDS_HUMAN → ACTIVE(显式人工确认,cli resume --ack-needs-human)。"""

        self._require_session(session_uuid)
        failure = self._store.query_one(
            "SELECT id FROM failures WHERE session_uuid = ? AND status = ?"
            " ORDER BY id DESC LIMIT 1",
            (session_uuid, FAILURE_NEEDS_HUMAN),
        )
        self._apply(
            session_uuid,
            ACTIVE,
            R_MANUAL_ACK,
            {
                "operator": operator,
                "failure_id": int(failure["id"]) if failure is not None else None,
            },
            expect_from=(NEEDS_HUMAN,),
        )
        self._audit_transition(
            session_uuid, ACTIVE, R_MANUAL_ACK, int(failure["id"]) if failure else None
        )

    # -- MISSING 覆盖 ------------------------------------------------------

    def mark_missing(self, session_uuid: str) -> None:
        """→ MISSING(rollout 文件缺失;detail 记录原状态以便恢复)。"""

        self._require_session(session_uuid)
        detail = dict(self._detail(session_uuid))
        current = self._states.state_of(session_uuid) or ACTIVE
        if current == MISSING:
            return
        detail["missing_from_state"] = current
        self._apply(
            session_uuid,
            MISSING,
            R_ROLLOUT_MISSING,
            {"previous_state": current},
            detail=detail,
        )
        self._audit_transition(session_uuid, MISSING, R_ROLLOUT_MISSING, None)

    def missing_recovered(self, session_uuid: str) -> str:
        """MISSING → 原状态(无记录则 ACTIVE);返回恢复后的状态。"""

        self._require_session(session_uuid)
        detail = self._detail(session_uuid)
        restore = detail.pop("missing_from_state", None)
        if restore in _EPHEMERAL_TERMINAL:
            restore = ACTIVE
        restore = restore or ACTIVE
        self._apply(
            session_uuid,
            restore,
            R_ROLLOUT_RESTORED,
            {"restored_state": restore},
            detail=detail or None,
            expect_from=(MISSING,),
        )
        self._audit_transition(session_uuid, restore, R_ROLLOUT_RESTORED, None)
        return restore

    def missing_over_grace(self, session_uuid: str, *, grace_sec: int) -> bool:
        """MISSING 超宽限期 → NEEDS_HUMAN;返回是否触发。"""

        row = self._states.get(session_uuid)
        if row is None or row["state"] != MISSING:
            return False
        now = self._clock.epoch()
        if now - row["since"] < grace_sec:
            return False
        # fail_to_human:关闭 open failure + 通用 _apply(任意来源态可达)落状态
        self.fail_to_human(
            session_uuid,
            R_MISSING_OVER_GRACE,
            {"missing_since": row["since"], "grace_sec": grace_sec},
        )
        return True

    # -- 投递相位(Phase 5 入口;P4 无调用方) ----------------------------

    def begin_delivery(self, session_uuid: str, failure_id: int, guards: dict[str, Any]) -> None:
        self._require_enabled(session_uuid)
        self._apply(
            session_uuid,
            DELIVERING,
            R_GUARDS_PASSED,
            {"failure_id": failure_id, "guards": guards},
            expect_from=(WAITING_RECOVERY,),
        )
        self._audit_transition(session_uuid, DELIVERING, R_GUARDS_PASSED, failure_id)

    def delivery_accepted(
        self, session_uuid: str, failure_id: int, receipt: dict[str, Any]
    ) -> None:
        self._apply(
            session_uuid,
            DELIVERED_UNCONFIRMED,
            R_QUEUE_ACCEPTED,
            {"failure_id": failure_id, "receipt": receipt},
            expect_from=(DELIVERING,),
        )
        self._audit_transition(session_uuid, DELIVERED_UNCONFIRMED, R_QUEUE_ACCEPTED, failure_id)

    def delivery_failed(self, session_uuid: str, reason: str, detail: dict[str, Any]) -> None:
        self._apply(session_uuid, NEEDS_HUMAN, reason, detail, expect_from=(DELIVERING,))
        self._audit_transition(session_uuid, NEEDS_HUMAN, reason, detail.get("failure_id"))

    def confirm_recovered(
        self, session_uuid: str, failure_id: int, method: str, evidence: dict[str, Any]
    ) -> None:
        """DELIVERED_UNCONFIRMED → RECOVERED → ACTIVE;failure → confirmed。"""

        now = self._clock.epoch()
        from codex_autopilot.store.recovery_dao import FAILURE_CONFIRMED

        with self._store.transaction() as conn:
            self._failures.set_status(failure_id, FAILURE_CONFIRMED, now=now, conn=conn)
            plan = conn.execute(
                "SELECT * FROM recovery_plans WHERE failure_id = ?", (failure_id,)
            ).fetchone()
            if plan is not None:
                self._plans.set_status(int(plan["id"]), PLAN_DELIVERED, now=now, conn=conn)
        self._apply(
            session_uuid,
            RECOVERED,
            R_NEW_TURN_CONFIRMED,
            {"failure_id": failure_id, "method": method, **evidence},
            expect_from=(DELIVERED_UNCONFIRMED,),
        )
        self._apply(
            session_uuid,
            ACTIVE,
            R_FAILURE_CLOSED_CONFIRMED,
            {"failure_id": failure_id},
            expect_from=(RECOVERED,),
        )
        self._audit_transition(session_uuid, RECOVERED, R_NEW_TURN_CONFIRMED, failure_id)

    # -- 审计 -------------------------------------------------------------

    def _audit_transition(
        self, session_uuid: str, to_state: str, reason: str, failure_id: int | None
    ) -> None:
        self._store.audit(
            actor="recovery:state-machine",
            category="state",
            level="info",
            summary=f"session -> {to_state} ({reason})",
            session_uuid=session_uuid,
            data={"to_state": to_state, "reason": reason, "failure_id": failure_id},
        )


def _plan_basis_windows(plan: sqlite3.Row) -> list[dict[str, Any]]:
    try:
        basis = json.loads(plan["basis"])
        windows = basis.get("windows")
        return windows if isinstance(windows, list) else []
    except (ValueError, AttributeError):
        return []


def _new_failure_row(session_uuid: str, failure: DetectedFailure):
    from codex_autopilot.store.recovery_dao import NewFailure

    return NewFailure(
        session_uuid=session_uuid,
        turn_id=failure.turn_id,
        kind=failure.kind,
        detected_at=failure.detected_at,
        evidence=dict(failure.evidence),
        blocking_window_keys=failure.blocking_window_keys,
    )
