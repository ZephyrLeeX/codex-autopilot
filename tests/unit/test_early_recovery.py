"""P4-T6 提前恢复信号单元测试:E1/E2/E3/E4(early-recovery §2)。

红线断言:E3 只观察 credits 状态字段,永不消费(consume 调用属黑名单,
T-GUARD-06 在集成层用 Fake 日志断言)。
"""

from __future__ import annotations

from codex_autopilot.codexif.appserver import NOTIFICATION_RATE_LIMITS_UPDATED
from codex_autopilot.quota.model import QuotaSnapshot, QuotaWindow, ResetCredits
from codex_autopilot.recovery.early import (
    SIGNAL_E1,
    SIGNAL_E2,
    SIGNAL_E3,
    SIGNAL_E4,
    detect_signals,
)


def snapshot(
    *,
    captured_at: int = 1000,
    ordinary: bool | None = None,
    windows: tuple[QuotaWindow, ...] = (),
    credits: ResetCredits | None = None,
) -> QuotaSnapshot:
    return QuotaSnapshot(
        source="appserver",
        captured_at=captured_at,
        ok=True,
        windows=windows,
        ordinary_usage_allowed=ordinary,
        reset_credits=credits,
    )


def window(used: float, resets: int, minutes: int = 300) -> QuotaWindow:
    return QuotaWindow(
        bucket_key=str(minutes),
        window_kind="five_hour" if minutes == 300 else "weekly",
        kind_inferred=False,
        used_percent=used,
        resets_at=resets,
        window_minutes=minutes,
    )


class TestE1:
    def test_false_to_true_emits_e1(self):
        signals = detect_signals(
            snapshot(ordinary=False), snapshot(ordinary=True, captured_at=2000), resume_max_pct=90
        )
        assert [s.signal for s in signals] == [SIGNAL_E1]

    def test_null_to_true_no_signal(self):
        """null → true 不算翻转(基线不可证)。"""

        signals = detect_signals(
            snapshot(ordinary=None), snapshot(ordinary=True), resume_max_pct=90
        )
        assert signals == []


class TestE2:
    def test_resets_at_rolled(self):
        base = snapshot(windows=(window(100, 1000),))
        curr = snapshot(windows=(window(10, 1000 + 18000),), captured_at=2000)
        signals = detect_signals(base, curr, resume_max_pct=90)
        assert [s.signal for s in signals] == [SIGNAL_E2]
        assert signals[0].detail["bucket_key"] == "300"

    def test_used_percent_dropped_below_resume_max(self):
        base = snapshot(windows=(window(99, 1000),))
        curr = snapshot(windows=(window(85, 1000),), captured_at=2000)  # resets 未变
        signals = detect_signals(base, curr, resume_max_pct=90)
        assert [s.signal for s in signals] == [SIGNAL_E2]

    def test_small_drop_no_signal(self):
        """下降但未跌破恢复下限:不算信号(需 > 恢复下限差值)。"""

        base = snapshot(windows=(window(99, 1000),))
        curr = snapshot(windows=(window(92, 1000),), captured_at=2000)
        assert detect_signals(base, curr, resume_max_pct=90) == []

    def test_no_change_no_signal(self):
        base = snapshot(windows=(window(100, 1000),))
        curr = snapshot(windows=(window(100, 1000),), captured_at=2000)
        assert detect_signals(base, curr, resume_max_pct=90) == []


class TestE3:
    def test_available_count_decrease(self):
        base = snapshot(
            credits=ResetCredits(available_count=2, statuses=("available", "available"))
        )
        curr = snapshot(
            credits=ResetCredits(available_count=1, statuses=("available",)), captured_at=2000
        )
        signals = detect_signals(base, curr, resume_max_pct=90)
        assert [s.signal for s in signals] == [SIGNAL_E3]

    def test_status_available_to_redeemed(self):
        base = snapshot(credits=ResetCredits(available_count=1, statuses=("available",)))
        curr = snapshot(
            credits=ResetCredits(available_count=1, statuses=("redeemed",)), captured_at=2000
        )
        signals = detect_signals(base, curr, resume_max_pct=90)
        assert [s.signal for s in signals] == [SIGNAL_E3]

    def test_no_change_no_signal(self):
        base = snapshot(credits=ResetCredits(available_count=1, statuses=("available",)))
        curr = snapshot(
            credits=ResetCredits(available_count=1, statuses=("available",)), captured_at=2000
        )
        assert detect_signals(base, curr, resume_max_pct=90) == []

    def test_missing_credits_no_signal(self):
        base = snapshot(credits=None)
        curr = snapshot(credits=ResetCredits(available_count=0), captured_at=2000)
        assert detect_signals(base, curr, resume_max_pct=90) == []


class TestE4:
    def test_notification_method_emits_e4(self):
        signals = detect_signals(
            snapshot(),
            snapshot(captured_at=2000),
            resume_max_pct=90,
            updated_methods=(NOTIFICATION_RATE_LIMITS_UPDATED,),
        )
        assert [s.signal for s in signals] == [SIGNAL_E4]

    def test_other_methods_ignored(self):
        signals = detect_signals(
            snapshot(),
            snapshot(captured_at=2000),
            resume_max_pct=90,
            updated_methods=("some/other/notification",),
        )
        assert signals == []
