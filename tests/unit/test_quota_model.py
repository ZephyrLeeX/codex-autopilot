"""P3-T2 测试:窗口模型与耗尽/阻塞判定(T-QUOTA-01..06;S-01..S-05、S-20、S-25)。"""

from __future__ import annotations

import pytest

from codex_autopilot.quota.model import (
    KIND_CREDITS,
    KIND_FIVE_HOUR,
    KIND_SPEND_CONTROL,
    KIND_UNKNOWN,
    KIND_WEEKLY,
    QuotaSnapshot,
    QuotaThresholds,
    QuotaWindow,
    apply_thresholds,
    assess_snapshot,
    make_bucket_key,
    resolve_window_kind,
)

NOW = 1767225600


def window(
    *,
    minutes=300,
    used=10.0,
    resets=NOW + 3600,
    kind=None,
    inferred=False,
    key=None,
    thresholds=None,
) -> QuotaWindow:
    """构造窗口并按阈值补全标记(与解析层同一实现)。"""

    thresholds = thresholds or QuotaThresholds()
    resolved_kind, resolved_inferred = resolve_window_kind(window_minutes=minutes, position=None)
    raw = QuotaWindow(
        bucket_key=key or make_bucket_key(minutes, 0),
        window_kind=kind if kind is not None else resolved_kind,
        kind_inferred=inferred or resolved_inferred,
        used_percent=used,
        resets_at=resets,
        window_minutes=minutes,
    )
    return apply_thresholds(raw, thresholds)


def snapshot(windows=(), **kwargs) -> QuotaSnapshot:
    defaults = {"source": "appserver", "captured_at": NOW, "ok": True}
    defaults.update(kwargs)
    return QuotaSnapshot(windows=tuple(windows), **defaults)


TH = QuotaThresholds()  # 默认:阈值 99,hint 下限 90


class TestResolveWindowKind:
    def test_minutes_authoritative(self):
        assert resolve_window_kind(window_minutes=300) == (KIND_FIVE_HOUR, False)
        assert resolve_window_kind(window_minutes=10080) == (KIND_WEEKLY, False)

    def test_position_inference_only_without_minutes(self):
        assert resolve_window_kind(window_minutes=None, position="primary") == (
            KIND_FIVE_HOUR,
            True,
        )
        assert resolve_window_kind(window_minutes=None, position="secondary") == (KIND_WEEKLY, True)

    def test_minutes_beats_position(self):
        # 分钟数存在时不推断,即使位置为 primary
        assert resolve_window_kind(window_minutes=10080, position="primary") == (KIND_WEEKLY, False)

    def test_explicit_window_type_field(self):
        assert resolve_window_kind(window_minutes=None, window_type="weekly") == (
            KIND_WEEKLY,
            False,
        )
        assert resolve_window_kind(window_minutes=None, window_type="weird_new_kind") == (
            KIND_UNKNOWN,
            False,
        )

    def test_nothing_known(self):
        assert resolve_window_kind(window_minutes=None, position="other") == (KIND_UNKNOWN, False)


class TestBucketKey:
    def test_minutes_based(self):
        assert make_bucket_key(300, 0) == "300"
        assert make_bucket_key(10080, 1) == "10080"

    def test_unknown_numbered(self):
        assert make_bucket_key(None, 0) == "unknown:0"
        assert make_bucket_key(None, 2) == "unknown:2"


class TestThresholds:
    def test_defaults(self):
        assert TH.exhausted_threshold_pct == 99
        assert TH.hint_min_pct == 90

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"exhausted_threshold_pct": 94},
            {"exhausted_threshold_pct": 101},
            {"hint_min_pct": 89},
            {"hint_min_pct": 99},  # hint ≥ 阈值(99)非法
        ],
    )
    def test_invalid(self, kwargs):
        with pytest.raises(ValueError):
            QuotaThresholds(**kwargs)


class TestAssessWindows:
    def test_normal_not_blocked(self):
        assessment = assess_snapshot(snapshot([window(minutes=300, used=47.0)]), thresholds=TH)
        assert not assessment.blocked and assessment.blocked_kind == "none"
        assert assessment.reference_resets_at is None

    def test_five_hour_99_exhausted(self):
        """S-02/T-QUOTA-02:99% 即耗尽,与 100% 同路径。"""

        for used in (99.0, 100.0):
            w = window(minutes=300, used=used, resets=NOW + 100)
            assessment = assess_snapshot(snapshot([w]), thresholds=TH)
            assert w.is_exhausted, used
            assert assessment.blocked and assessment.blocked_kind == "windows"
            assert assessment.blocking_keys == ("300",)
            assert assessment.reference_resets_at == NOW + 100

    def test_weekly_exhausted(self):
        """S-03/S-04:T-QUOTA-03/04 secondary(10080)判定。"""

        for used in (99.0, 100.0):
            w = window(minutes=10080, used=used, resets=NOW + 9999)
            assessment = assess_snapshot(snapshot([w]), thresholds=TH)
            assert assessment.blocked and assessment.blocking_keys == ("10080",)
            assert assessment.reference_resets_at == NOW + 9999

    def test_dual_window_both_block_reference_is_max(self):
        """S-05/T-QUOTA-05:双窗口耗尽,参考 = max(resets_at)。"""

        w5 = window(minutes=300, used=99.5, resets=NOW + 100)
        w7 = window(minutes=10080, used=100.0, resets=NOW + 9999)
        assessment = assess_snapshot(snapshot([w5, w7]), thresholds=TH)
        assert assessment.blocked
        assert set(assessment.blocking_keys) == {"300", "10080"}
        assert assessment.reference_resets_at == NOW + 9999  # 等最晚

    def test_hint_range_not_blocking(self):
        w = window(minutes=300, used=95.0)
        assessment = assess_snapshot(snapshot([w]), thresholds=TH)
        assert w.hint and not w.is_exhausted
        assert not assessment.blocked  # 90-98 仅 hint,不作耗尽判定

    def test_below_hint_no_flag(self):
        w = window(minutes=300, used=50.0)
        assess_snapshot(snapshot([w]), thresholds=TH)
        assert not w.hint and not w.is_exhausted

    def test_higher_threshold_config(self):
        thresholds = QuotaThresholds(exhausted_threshold_pct=95)
        w = window(minutes=300, used=96.0, thresholds=thresholds)
        assessment = assess_snapshot(snapshot([w]), thresholds=thresholds)
        assert w.is_exhausted and assessment.blocked


class TestAssessConservative:
    def test_unknown_window_kind_blocks(self):
        """S-25/T-COMPAT-01:未知窗口类型保守阻塞。"""

        w = window(minutes=20160, used=41.0, resets=NOW + 1, kind=KIND_UNKNOWN)
        assessment = assess_snapshot(snapshot([w]), thresholds=TH)
        assert assessment.blocked and assessment.blocked_kind == KIND_UNKNOWN
        assert assessment.unknown_reason == "unverifiable_window"

    def test_missing_used_percent_blocks(self):
        w = window(minutes=300, used=None, resets=NOW + 60)
        assessment = assess_snapshot(snapshot([w]), thresholds=TH)
        assert assessment.blocked and assessment.blocked_kind == KIND_UNKNOWN

    def test_missing_resets_at_no_reference(self):
        w = window(minutes=300, used=100.0, resets=None)
        assessment = assess_snapshot(snapshot([w]), thresholds=TH)
        assert assessment.blocked
        assert assessment.reference_resets_at is None  # 恢复时间未知,不得推算

    def test_failed_snapshot_blocks(self):
        assessment = assess_snapshot(snapshot(ok=False, error="boom"), thresholds=TH)
        assert assessment.blocked and assessment.blocked_kind == KIND_UNKNOWN
        assert assessment.unknown_reason == "snapshot_not_ok"

    def test_empty_snapshot_with_allowed_true_not_blocked(self):
        assessment = assess_snapshot(snapshot(ordinary_usage_allowed=True), thresholds=TH)
        assert not assessment.blocked

    def test_empty_snapshot_unknown_when_allowed_missing(self):
        """S-20/T-QUOTA-09:快照缺失(空)不得推断为已恢复。"""

        assessment = assess_snapshot(snapshot(ordinary_usage_allowed=None), thresholds=TH)
        assert assessment.blocked and assessment.blocked_kind == KIND_UNKNOWN


class TestAssessCreditsAndFlags:
    def test_credits_depleted_blocks_without_resets(self):
        """credits/workspace 类:阻塞且无恢复时间(只能人工/官方改变)。"""

        snap = snapshot(
            [window(minutes=300, used=12.0)],
            rate_limit_reached_type="workspace_owner_credits_depleted",
        )
        assessment = assess_snapshot(snap, thresholds=TH)
        assert assessment.blocked and assessment.blocked_kind == KIND_CREDITS
        assert assessment.blocking_keys == () and assessment.reference_resets_at is None

    def test_member_credits_variant(self):
        snap = snapshot(
            rate_limit_reached_type="workspace_member_credits_depleted",
            ordinary_usage_allowed=False,
        )
        assessment = assess_snapshot(snap, thresholds=TH)
        assert assessment.blocked_kind == KIND_CREDITS

    def test_spend_control_blocks(self):
        snap = snapshot([window(minutes=300, used=5.0)], spend_control_reached=True)
        assessment = assess_snapshot(snap, thresholds=TH)
        assert assessment.blocked and assessment.blocked_kind == KIND_SPEND_CONTROL

    def test_rate_limit_reached_type_alone_is_window_path(self):
        snap = snapshot(
            [window(minutes=300, used=41.0)], rate_limit_reached_type="rate_limit_reached"
        )
        assessment = assess_snapshot(snap, thresholds=TH)
        assert not assessment.blocked  # 非金额类标志不单独阻塞,窗口说了算

    def test_ordinary_false_blocks_even_with_clean_windows(self):
        snap = snapshot([window(minutes=300, used=1.0)], ordinary_usage_allowed=False)
        assessment = assess_snapshot(snap, thresholds=TH)
        assert assessment.blocked and assessment.blocked_kind == KIND_UNKNOWN
        assert assessment.unknown_reason == "ordinary_usage_not_allowed"

    def test_ordinary_true_does_not_unblock_exhausted_window(self):
        """99%≠可用:服务器预扣场景,ordinaryUsageAllowed=true 不解除窗口阻塞。"""

        snap = snapshot([window(minutes=300, used=100.0)], ordinary_usage_allowed=True)
        assessment = assess_snapshot(snap, thresholds=TH)
        assert assessment.blocked and assessment.blocked_kind == "windows"
        assert assessment.ordinary_usage_allowed is True

    def test_ordinary_true_unblocks_when_windows_clean(self):
        snap = snapshot([window(minutes=300, used=10.0)], ordinary_usage_allowed=True)
        assessment = assess_snapshot(snap, thresholds=TH)
        assert not assessment.blocked
