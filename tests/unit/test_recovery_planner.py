"""P4-T4 恢复计划器单元测试:not_before 计算、不确定恢复时间、异常时间戳。

对应设计:early-recovery §1、quota-monitoring §5.1、任务书 FR-03-A8;
场景 S-05(双窗口 max-resets)。
"""

from __future__ import annotations

from codex_autopilot.recovery.model import (
    KIND_QUOTA_FIVE_HOUR,
    KIND_QUOTA_MULTI,
    DetectedFailure,
    WindowEvidence,
)
from codex_autopilot.recovery.planner import RESET_AT_SKEW_SEC, RecoveryPlanner
from tests import helpers as h


def failure(
    *,
    kind: str = KIND_QUOTA_FIVE_HOUR,
    windows: tuple[WindowEvidence, ...],
    sample_ts: int | None = h.T0 + 60,
) -> DetectedFailure:
    return DetectedFailure(
        turn_id="t-1",
        kind=kind,
        detected_at=h.T0 + 100,
        blocking_window_keys=tuple(w.bucket_key for w in windows),
        windows=windows,
        evidence={"snapshot_captured_at": sample_ts},
    )


def win(resets_at: int | None, minutes: int = 300, used: float = 100.0) -> WindowEvidence:
    return WindowEvidence(
        bucket_key=str(minutes),
        window_kind="five_hour" if minutes == 300 else "weekly",
        used_percent=used,
        resets_at=resets_at,
        window_minutes=minutes,
    )


class TestWindowReset:
    def test_not_before_is_window_reset(self, fake_clock):
        planned = RecoveryPlanner(fake_clock).plan(failure(windows=(win(h.T0 + 18000),)))
        assert planned.strategy == "window_reset"
        assert planned.not_before == h.T0 + 18000

    def test_dual_window_takes_max(self, fake_clock):
        """S-05:双窗口阻塞 → 参考时间 = max(resets_at)(等最晚)。"""

        planned = RecoveryPlanner(fake_clock).plan(
            failure(
                kind=KIND_QUOTA_MULTI,
                windows=(win(h.T0 + 18000), win(h.T0 + 500000, minutes=10080)),
            )
        )
        assert planned.not_before == h.T0 + 500000
        basis_windows = planned.basis["windows"]
        assert [w["bucket_key"] for w in basis_windows] == ["300", "10080"]

    def test_expired_resets_at_eligible_now(self, fake_clock):
        """已过期时间戳:参考立即到期(恢复仍需现场证据,非授权)。"""

        planned = RecoveryPlanner(fake_clock).plan(failure(windows=(win(h.T0 - 9999),)))
        assert planned.strategy == "window_reset"
        assert planned.not_before == fake_clock.epoch()
        assert any("expired" in note for note in planned.basis["notes"])

    def test_far_future_resets_at_capped(self, fake_clock):
        """异常偏晚:超出 快照时刻+窗口长度+容差 → 截断并标记。"""

        sample_ts = h.T0 + 60
        ceiling = sample_ts + 300 * 60 + RESET_AT_SKEW_SEC
        planned = RecoveryPlanner(fake_clock).plan(
            failure(
                windows=(
                    win(
                        ceiling + 7200,
                    ),
                ),
                sample_ts=sample_ts,
            )
        )
        assert planned.strategy == "window_reset"
        assert planned.not_before == ceiling
        assert any("capped" in note for note in planned.basis["notes"])


class TestUncertainRecoveryTime:
    def test_missing_resets_at_falls_to_manual_ack(self, fake_clock):
        """任一阻塞窗口缺 resets_at ⇒ 整体不可靠 ⇒ manual_ack(fail-closed)。"""

        planned = RecoveryPlanner(fake_clock).plan(
            failure(kind=KIND_QUOTA_MULTI, windows=(win(h.T0 + 100), win(None)))
        )
        assert planned.strategy == "manual_ack"
        assert planned.basis["reason"] == "missing_or_invalid_resets_at"

    def test_non_positive_resets_at_manual_ack(self, fake_clock):
        planned = RecoveryPlanner(fake_clock).plan(
            failure(
                windows=(
                    win(
                        0,
                    ),
                )
            )
        )
        assert planned.strategy == "manual_ack"

    def test_credits_kind_manual_ack(self, fake_clock):
        planned = RecoveryPlanner(fake_clock).plan(
            DetectedFailure(
                turn_id="t",
                kind="quota_credits",
                detected_at=h.T0,
                evidence={},
            )
        )
        assert planned.strategy == "manual_ack"
        assert planned.not_before == fake_clock.epoch()

    def test_unknown_kind_manual_ack(self, fake_clock):
        planned = RecoveryPlanner(fake_clock).plan(
            DetectedFailure(turn_id="t", kind="quota_unknown", detected_at=h.T0, evidence={})
        )
        assert planned.strategy == "manual_ack"

    def test_window_kind_without_windows_manual_ack(self, fake_clock):
        """窗口类失败但无窗口证据(异常路径)⇒ manual_ack。"""

        planned = RecoveryPlanner(fake_clock).plan(failure(windows=(), sample_ts=None))
        assert planned.strategy == "manual_ack"
