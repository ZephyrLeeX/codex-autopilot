"""守护进程/调度周期启动清扫(persistence.md §7;P4-T5)。

步骤(任何一步失败都进入受限模式 —— 返回 restricted=True,调度器冻结
一切自动动作,交 doctor/人工处理):
1. `PRAGMA integrity_check` ≠ ok → 受限(S-21);
2. 未决投递清扫:attempting → unknown + failure → NEEDS_HUMAN
   (FR-08-A7:宁人工确认,不冒双投风险);
3. 孤儿计划清扫:failure 已终态但 plan 仍 waiting → plan expired;
4. 状态自洽检查:session_state 的 detail 指向的 failure/plan 必须存在且
   状态一致;不一致 → 该会话 NEEDS_HUMAN(db_inconsistent,不猜测修复);
5. WAL 检查点 + 启动审计。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from codex_autopilot.recovery.state_machine import (
    R_DB_INCONSISTENT,
    RecoveryStateMachine,
    StateMachineError,
)
from codex_autopilot.store import Store
from codex_autopilot.store.recovery_dao import (
    PLAN_WAITING,
    DeliveryDao,
    RecoveryPlanDao,
)
from codex_autopilot.util.clock import TimeSource


@dataclass(frozen=True)
class SweepResult:
    ok: bool
    restricted_reason: str | None = None
    swept_deliveries: tuple[int, ...] = ()
    expired_plans: tuple[int, ...] = ()
    inconsistent_sessions: tuple[str, ...] = ()
    faults: tuple[str, ...] = field(default_factory=tuple)


def startup_sweep(store: Store, clock: TimeSource, *, actor: str = "daemon") -> SweepResult:
    now = clock.epoch()
    faults: list[str] = []

    # 1) 完整性
    if not store.integrity_ok():
        store.audit(
            actor=actor,
            category="fault",
            level="error",
            summary="integrity_check failed; entering restricted mode",
            data={"stage": "sweep"},
        )
        return SweepResult(ok=False, restricted_reason="integrity_check_failed")

    # 2) 未决投递
    delivery_dao = DeliveryDao(store)
    swept = delivery_dao.sweep_attempting(now=now, actor="recovery:sweep")
    if swept:
        store.audit(
            actor=actor,
            category="delivery",
            level="warn",
            summary=f"swept {len(swept)} attempting delivery(s) to unknown (needs_human)",
            data={"delivery_ids": swept},
        )

    # 3) 孤儿计划
    plan_dao = RecoveryPlanDao(store)
    orphans = store.query(
        "SELECT p.id FROM recovery_plans p JOIN failures f ON f.id = p.failure_id"
        " WHERE p.status = ? AND f.status NOT IN ('open', 'planned', 'delivering')",
        (PLAN_WAITING,),
    )
    expired: list[int] = []
    for row in orphans:
        plan_dao.set_status(int(row["id"]), "expired", now=now)
        expired.append(int(row["id"]))
    if expired:
        store.audit(
            actor=actor,
            category="state",
            level="info",
            summary=f"expired {len(expired)} orphan recovery plan(s)",
            data={"plan_ids": expired},
        )

    # 4) 状态自洽(session_state.detail 的 failure/plan 引用)
    machine = RecoveryStateMachine(store, clock)
    inconsistent: list[str] = []
    for state_row in store.query("SELECT * FROM session_state"):
        detail = {}
        if state_row["detail"]:
            try:
                parsed = json.loads(state_row["detail"])
                detail = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                faults.append(f"unparseable state detail: {state_row['session_uuid']}")
        failure_id = detail.get("failure_id")
        if failure_id is None:
            continue
        failure = store.query_one("SELECT * FROM failures WHERE id = ?", (failure_id,))
        expected_state = state_row["state"]
        consistent = failure is not None and failure["session_uuid"] == state_row["session_uuid"]
        if consistent and expected_state == "WAITING_RECOVERY":
            consistent = failure["status"] == "planned"
        if not consistent:
            inconsistent.append(state_row["session_uuid"])
            try:
                machine.fail_to_human(
                    state_row["session_uuid"],
                    R_DB_INCONSISTENT,
                    {
                        "failure_id": failure_id,
                        "state": expected_state,
                        "failure_status": failure["status"] if failure else None,
                    },
                )
            except StateMachineError as exc:
                faults.append(f"cannot mark inconsistent session: {exc}")

    # 5) WAL 检查点 + 审计
    store.checkpoint_wal()
    store.audit(
        actor=actor,
        category="tick",
        level="info",
        summary=(
            f"startup sweep ok (swept={len(swept)} expired_plans={len(expired)}"
            f" inconsistent={len(inconsistent)})"
        ),
        data={
            "swept_deliveries": swept,
            "expired_plans": expired,
            "inconsistent_sessions": inconsistent,
        },
    )
    ok = not faults
    return SweepResult(
        ok=ok,
        restricted_reason=None if ok else "sweep_faults",
        swept_deliveries=tuple(swept),
        expired_plans=tuple(expired),
        inconsistent_sessions=tuple(inconsistent),
        faults=tuple(faults),
    )


def connection_alive(store: Store) -> bool:
    """轻量健康检查(每 tick G11 用):连接可用即可。

    integrity_check 仅在启动清扫执行(§7 步骤 1);每 tick 跑全库检查
    对大库过重,G11 的完整语义 = 启动清扫通过 + 本 tick 无 fault 记录。
    """

    try:
        row = store.query_one("SELECT 1")
        return row is not None
    except sqlite3.Error:
        return False
