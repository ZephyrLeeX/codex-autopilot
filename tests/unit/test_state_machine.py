"""P4-T3 状态机单元测试:全部迁移、守卫失败分支、fail-closed、覆盖层。

对应设计:recovery-state-machine §2/§3/§5/§7;S-14/15/24 状态侧。
"""

from __future__ import annotations

import json

import pytest

from codex_autopilot.recovery.model import DetectedFailure, WindowEvidence
from codex_autopilot.recovery.state_machine import (
    ACTIVE,
    FAILURE_DETECTED,
    MISSING,
    NEEDS_HUMAN,
    WAITING_RECOVERY,
    InvalidTransitionError,
    RecoveryStateMachine,
    SessionPausedError,
    effective_state,
)
from codex_autopilot.store.dao import AccountDao, NewSession, SessionDao
from codex_autopilot.store.recovery_dao import FailureDao, RecoveryPlanDao

UUID = "01234567-89ab-cdef-0123-456789abcdef"


@pytest.fixture
def machine(store, fake_clock):
    return RecoveryStateMachine(store, fake_clock)


def register(store, fake_clock, *, enabled=True, auto_resume=True) -> None:
    accounts = AccountDao(store)
    with store.transaction():
        account = accounts.get_or_create("/tmp/codex-home", label=None, now=fake_clock.epoch())
        SessionDao(store).insert(
            NewSession(
                uuid=UUID,
                name="alpha",
                project_path="/tmp/project",
                account_id=int(account["id"]),
                auto_resume=auto_resume,
            ),
            now=fake_clock.epoch(),
        )
    if not enabled:
        store.execute("UPDATE sessions SET enabled = 0 WHERE uuid = ?", (UUID,))


def quota_failure(now: int, **overrides) -> DetectedFailure:
    base = {
        "turn_id": "t-1",
        "kind": "quota_five_hour",
        "detected_at": now,
        "blocking_window_keys": ("300",),
        "windows": (
            WindowEvidence(
                bucket_key="300",
                window_kind="five_hour",
                used_percent=100.0,
                resets_at=now + 18000,
                window_minutes=300,
            ),
        ),
        "evidence": {"source": "jsonl", "snapshot_captured_at": now - 60},
    }
    base.update(overrides)
    return DetectedFailure(**base)


class TestRecordFailure:
    def test_quota_failure_goes_to_failure_detected(self, store, fake_clock, machine):
        register(store, fake_clock)
        row, action = machine.record_failure_for(UUID, quota_failure(fake_clock.epoch()))
        assert action == "failure_detected"
        assert machine.state_of(UUID) == FAILURE_DETECTED
        assert row["status"] == "open"
        history = store.query("SELECT * FROM state_history ORDER BY id")
        assert history[-1]["to_state"] == FAILURE_DETECTED
        assert history[-1]["reason"] == "failure_detected"
        assert json.loads(history[-1]["evidence"])["failure_id"] == row["id"]

    def test_aborted_goes_straight_to_needs_human(self, store, fake_clock, machine):
        register(store, fake_clock)
        row, action = machine.record_failure_for(
            UUID,
            quota_failure(
                fake_clock.epoch(),
                kind="aborted",
                blocking_window_keys=(),
                windows=(),
                evidence={"error_variant": "aborted"},
            ),
        )
        assert action == "needs_human"
        assert machine.state_of(UUID) == NEEDS_HUMAN
        assert row["status"] == "needs_human"
        assert row["closed_at"] is not None

    def test_duplicate_turn_is_idempotent(self, store, fake_clock, machine):
        register(store, fake_clock)
        first, action = machine.record_failure_for(UUID, quota_failure(fake_clock.epoch()))
        assert action == "failure_detected"
        again, action2 = machine.record_failure_for(UUID, quota_failure(fake_clock.epoch() + 10))
        assert action2 == "duplicate"
        assert again["id"] == first["id"]
        assert store.query_one("SELECT COUNT(*) c FROM failures")["c"] == 1

    def test_record_fails_when_not_active(self, store, fake_clock, machine):
        register(store, fake_clock)
        machine.record_failure_for(UUID, quota_failure(fake_clock.epoch()))
        with pytest.raises(InvalidTransitionError):
            machine.record_failure_for(UUID, quota_failure(fake_clock.epoch() + 5, turn_id="t-2"))

    def test_record_frozen_when_paused(self, store, fake_clock, machine):
        register(store, fake_clock, enabled=False)
        with pytest.raises(SessionPausedError):
            machine.record_failure_for(UUID, quota_failure(fake_clock.epoch()))
        assert store.query_one("SELECT COUNT(*) c FROM failures")["c"] == 0


class TestPlanAndWaiting:
    def _detected(self, store, fake_clock, machine):
        register(store, fake_clock)
        failure, _ = machine.record_failure_for(UUID, quota_failure(fake_clock.epoch()))
        plan = RecoveryPlanDao(store).insert(
            int(failure["id"]),
            strategy="window_reset",
            not_before=fake_clock.epoch() + 18000,
            basis={"windows": []},
            now=fake_clock.epoch(),
        )
        return failure, plan

    def test_arm_plan_transitions_to_waiting(self, store, fake_clock, machine):
        failure, plan = self._detected(store, fake_clock, machine)
        machine.arm_plan(UUID, int(failure["id"]), int(plan["id"]))
        assert machine.state_of(UUID) == WAITING_RECOVERY
        detail = json.loads(machine.current(UUID)["detail"])
        assert detail == {"failure_id": int(failure["id"]), "plan_id": int(plan["id"])}
        assert FailureDao(store).get(int(failure["id"]))["status"] == "planned"

    def test_arm_requires_open_failure(self, store, fake_clock, machine):
        failure, plan = self._detected(store, fake_clock, machine)
        store.execute("UPDATE failures SET status='superseded' WHERE id = ?", (failure["id"],))
        with pytest.raises(Exception, match="not open"):
            machine.arm_plan(UUID, int(failure["id"]), int(plan["id"]))

    def test_arm_rejects_from_wrong_state(self, store, fake_clock, machine):
        failure, plan = self._detected(store, fake_clock, machine)
        machine.arm_plan(UUID, int(failure["id"]), int(plan["id"]))
        with pytest.raises(InvalidTransitionError):
            machine.arm_plan(UUID, int(failure["id"]), int(plan["id"]))


class TestSupersede:
    def test_supersede_closes_failure_and_returns_active(self, store, fake_clock, machine):
        register(store, fake_clock)
        failure, _ = machine.record_failure_for(UUID, quota_failure(fake_clock.epoch()))
        plan = RecoveryPlanDao(store).insert(
            int(failure["id"]),
            strategy="window_reset",
            not_before=fake_clock.epoch() + 9999,
            basis={},
            now=fake_clock.epoch(),
        )
        machine.arm_plan(UUID, int(failure["id"]), int(plan["id"]))
        machine.supersede(
            UUID,
            {"messages": [{"timestamp": fake_clock.epoch(), "head": "manual continue"}]},
        )
        assert machine.state_of(UUID) == ACTIVE
        assert FailureDao(store).get(int(failure["id"]))["status"] == "superseded"
        assert RecoveryPlanDao(store).get(int(plan["id"]))["status"] == "cancelled"
        reasons = [row["reason"] for row in store.query("SELECT reason FROM state_history")]
        assert "human_message_seen" in reasons
        assert "failure_closed_superseded" in reasons

    def test_supersede_without_open_failure_rejected(self, store, fake_clock, machine):
        register(store, fake_clock)
        with pytest.raises(Exception, match="no open failure"):
            machine.supersede(UUID, {"messages": []})

    def test_supersede_frozen_when_paused(self, store, fake_clock, machine):
        register(store, fake_clock)
        _failure, _ = machine.record_failure_for(UUID, quota_failure(fake_clock.epoch()))
        store.execute("UPDATE sessions SET enabled = 0 WHERE uuid = ?", (UUID,))
        with pytest.raises(SessionPausedError):
            machine.supersede(UUID, {"messages": []})


class TestAckNeedsHuman:
    def test_ack_returns_to_active(self, store, fake_clock, machine):
        register(store, fake_clock)
        machine.record_failure_for(
            UUID,
            quota_failure(fake_clock.epoch(), kind="unknown_error", evidence={}),
        )
        assert machine.state_of(UUID) == NEEDS_HUMAN
        machine.ack_needs_human(UUID, operator="cli:resume")
        assert machine.state_of(UUID) == ACTIVE
        last = store.query_one("SELECT * FROM state_history ORDER BY id DESC")
        assert last["reason"] == "manual_ack"
        assert json.loads(last["evidence"])["operator"] == "cli:resume"

    def test_ack_requires_needs_human_state(self, store, fake_clock, machine):
        register(store, fake_clock)
        with pytest.raises(InvalidTransitionError):
            machine.ack_needs_human(UUID, operator="x")


class TestMissing:
    def test_missing_and_restore_roundtrip(self, store, fake_clock, machine):
        register(store, fake_clock)
        machine.mark_missing(UUID)
        assert machine.state_of(UUID) == MISSING
        restored = machine.missing_recovered(UUID)
        assert restored == ACTIVE
        assert machine.state_of(UUID) == ACTIVE

    def test_missing_restores_pre_missing_state(self, store, fake_clock, machine):
        register(store, fake_clock)
        failure, _ = machine.record_failure_for(UUID, quota_failure(fake_clock.epoch()))
        plan = RecoveryPlanDao(store).insert(
            int(failure["id"]),
            strategy="window_reset",
            not_before=1,
            basis={},
            now=fake_clock.epoch(),
        )
        machine.arm_plan(UUID, int(failure["id"]), int(plan["id"]))
        machine.mark_missing(UUID)
        assert machine.state_of(UUID) == MISSING
        assert machine.missing_recovered(UUID) == WAITING_RECOVERY

    def test_missing_grace_expires_to_needs_human(self, store, fake_clock, machine):
        register(store, fake_clock)
        machine.mark_missing(UUID)
        assert not machine.missing_over_grace(UUID, grace_sec=86400)
        fake_clock.advance(86401)
        assert machine.missing_over_grace(UUID, grace_sec=86400)
        assert machine.state_of(UUID) == NEEDS_HUMAN

    def test_missing_is_idempotent(self, store, fake_clock, machine):
        register(store, fake_clock)
        machine.mark_missing(UUID)
        machine.mark_missing(UUID)
        assert machine.state_of(UUID) == MISSING
        assert (
            store.query_one("SELECT COUNT(*) c FROM state_history WHERE to_state = 'MISSING'")["c"]
            == 1
        )


class TestFailToHuman:
    def test_closes_open_failure_and_cancels_plan(self, store, fake_clock, machine):
        register(store, fake_clock)
        failure, _ = machine.record_failure_for(UUID, quota_failure(fake_clock.epoch()))
        plan = RecoveryPlanDao(store).insert(
            int(failure["id"]),
            strategy="window_reset",
            not_before=1,
            basis={},
            now=fake_clock.epoch(),
        )
        machine.arm_plan(UUID, int(failure["id"]), int(plan["id"]))
        machine.fail_to_human(UUID, "unknown_quota_kind", {"failure_id": int(failure["id"])})
        assert machine.state_of(UUID) == NEEDS_HUMAN
        assert FailureDao(store).get(int(failure["id"]))["status"] == "needs_human"
        assert RecoveryPlanDao(store).get(int(plan["id"]))["status"] == "cancelled"


class TestEffectiveState:
    def test_paused_overlay(self, store, fake_clock, machine):
        register(store, fake_clock)
        _failure, _ = machine.record_failure_for(UUID, quota_failure(fake_clock.epoch()))
        state_row = machine.current(UUID)
        assert effective_state(state_row, enabled=True) == FAILURE_DETECTED
        assert effective_state(state_row, enabled=False) == "PAUSED"
        assert effective_state(None, enabled=True) == ACTIVE
        assert effective_state(None, enabled=False) == "PAUSED"


class TestFailClosedPersistence:
    def test_invalid_transition_leaves_no_partial_writes(self, store, fake_clock, machine):
        """迁移失败(非法 from)时:history 与 state 都不写(原子回滚)。"""

        register(store, fake_clock)
        machine.record_failure_for(UUID, quota_failure(fake_clock.epoch()))
        before_history = store.query_one("SELECT COUNT(*) c FROM state_history")["c"]
        with pytest.raises(Exception, match="not open"):
            machine.arm_plan(UUID, 999999, 999999)  # 失败行不存在
        failure_id = store.query_one("SELECT id FROM failures")["id"]
        plan = RecoveryPlanDao(store).insert(
            failure_id, strategy="window_reset", not_before=1, basis={}, now=fake_clock.epoch()
        )
        machine.arm_plan(UUID, failure_id, int(plan["id"]))
        history_at_waiting = store.query_one("SELECT COUNT(*) c FROM state_history")["c"]
        with pytest.raises(InvalidTransitionError):
            machine.arm_plan(UUID, failure_id, int(plan["id"]))
        assert store.query_one("SELECT COUNT(*) c FROM state_history")["c"] == history_at_waiting
        assert before_history >= 1
