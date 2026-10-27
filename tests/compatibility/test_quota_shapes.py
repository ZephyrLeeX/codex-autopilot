"""P3-T2 兼容性测试:多版本字段形状 fixture 驱动解析器(T-COMPAT-01;S-25)。

Fixture 全部人工构造/脱敏(testing.md §1 红线);形状依据 phase0-research.md
双源实证:appserver camelCase + rateLimitsByLimitId/rateLimits,JSONL snake_case。
"""

from __future__ import annotations

import json

import pytest

from codex_autopilot.quota.model import (
    KIND_CREDITS,
    KIND_FIVE_HOUR,
    KIND_SPEND_CONTROL,
    KIND_UNKNOWN,
    KIND_WEEKLY,
    QuotaThresholds,
    assess_snapshot,
)
from codex_autopilot.quota.parser import parse_appserver_response, parse_jsonl_rate_limits

TH = QuotaThresholds()
NOW = 1767225600


def load(fixture_path, *parts):
    return json.loads(fixture_path("appserver", *parts).read_text(encoding="utf-8"))


def windows_by_kind(snapshot):
    return {w.window_kind: w for w in snapshot.windows}


class TestAppServerShapes:
    def test_by_limit_id_new_shape(self, fixture_path):
        """新版键 rateLimitsByLimitId:分桶、premium 全 null 整桶丢弃。"""

        snapshot = parse_appserver_response(
            load(fixture_path, "by_limit_id.json"), thresholds=TH, captured_at=NOW
        )
        assert snapshot.ok
        kinds = windows_by_kind(snapshot)
        assert set(kinds) == {KIND_FIVE_HOUR, KIND_WEEKLY}  # premium 被丢
        assert kinds[KIND_FIVE_HOUR].used_percent == 98.0
        assert kinds[KIND_FIVE_HOUR].limit_id == "codex"
        assert kinds[KIND_WEEKLY].window_minutes == 10080
        assert snapshot.ordinary_usage_allowed is True
        assert snapshot.account_email == "user@example.com"
        assert snapshot.plan_type == "team"

    def test_by_limit_id_98_percent_not_exhausted_at_default(self, fixture_path):
        """98% 在阈值 99 下仅 hint:严格区分使用率与阻塞。"""

        snapshot = parse_appserver_response(
            load(fixture_path, "by_limit_id.json"), thresholds=TH, captured_at=NOW
        )
        five = windows_by_kind(snapshot)[KIND_FIVE_HOUR]
        assert not five.is_exhausted and five.hint
        assert not assess_snapshot(snapshot, thresholds=TH).blocked

    def test_legacy_rate_limits_fallback(self, fixture_path):
        """旧版键 rateLimits(单快照,无桶名)。"""

        snapshot = parse_appserver_response(
            load(fixture_path, "legacy_rate_limits.json"), thresholds=TH, captured_at=NOW
        )
        assert snapshot.ok
        kinds = windows_by_kind(snapshot)
        assert kinds[KIND_FIVE_HOUR].used_percent == 15.0
        assert kinds[KIND_FIVE_HOUR].limit_id is None
        assert snapshot.ordinary_usage_allowed is False

    def test_multi_bucket_real(self, fixture_path):
        """多额度桶并存:每桶各自窗口;同分钟 bucket_key 重复但 limit_id 区分(S-09 解析侧)。"""

        snapshot = parse_appserver_response(
            load(fixture_path, "multi_bucket_real.json"), thresholds=TH, captured_at=NOW
        )
        assert snapshot.ok
        assert len(snapshot.windows) == 4
        five_hour = [w for w in snapshot.windows if w.window_kind == KIND_FIVE_HOUR]
        assert {w.limit_id for w in five_hour} == {"codex", "legacy"}
        keys = [w.bucket_key for w in five_hour]
        assert keys == ["300", "300"]  # 跨源稳定 key,重复由 limit_id 区分
        # codex 桶 100% 阻塞;评估给出 max resets
        assessment = assess_snapshot(snapshot, thresholds=TH)
        assert assessment.blocked and assessment.blocking_keys == ("300",)

    def test_credits_depleted(self, fixture_path):
        snapshot = parse_appserver_response(
            load(fixture_path, "credits_depleted.json"), thresholds=TH, captured_at=NOW
        )
        assert snapshot.rate_limit_reached_type == "workspace_owner_credits_depleted"
        assessment = assess_snapshot(snapshot, thresholds=TH)
        assert assessment.blocked_kind == KIND_CREDITS

    def test_spend_control(self, fixture_path):
        snapshot = parse_appserver_response(
            load(fixture_path, "spend_control.json"), thresholds=TH, captured_at=NOW
        )
        assert snapshot.spend_control_reached
        assessment = assess_snapshot(snapshot, thresholds=TH)
        assert assessment.blocked_kind == KIND_SPEND_CONTROL

    def test_empty_response(self, fixture_path):
        """空快照:ok 但无窗口;ordinaryUsageAllowed 决定是否视为阻塞。"""

        snapshot = parse_appserver_response(
            load(fixture_path, "empty.json"), thresholds=TH, captured_at=NOW
        )
        assert snapshot.ok and snapshot.windows == ()
        assert not assess_snapshot(snapshot, thresholds=TH).blocked  # allowed=true

    def test_null_minutes_position_inference(self, fixture_path):
        """windowDurationMins=null:仅按位置推断,必须标记 inferred(FR-03-A2)。"""

        snapshot = parse_appserver_response(
            load(fixture_path, "null_fields.json"), thresholds=TH, captured_at=NOW
        )
        kinds = windows_by_kind(snapshot)
        five = kinds[KIND_FIVE_HOUR]
        assert five.kind_inferred and five.window_minutes is None
        assert five.resets_at is None  # resetsAt 也为 null,保留为不可评估信息
        assert five.bucket_key == "unknown:0"

    def test_snake_case_keys_in_appserver_response(self, fixture_path):
        """D-QM-1:同一解析器接受 camelCase 与 snake_case 两套键名。"""

        snapshot = parse_appserver_response(
            load(fixture_path, "snake_case_keys.json"), thresholds=TH, captured_at=NOW
        )
        assert snapshot.ok
        kinds = windows_by_kind(snapshot)
        assert kinds[KIND_FIVE_HOUR].used_percent == 91.0
        assert kinds[KIND_FIVE_HOUR].hint  # 91 ∈ [90,98)

    def test_unknown_window_type_conservative(self, fixture_path):
        """S-25/T-COMPAT-01:未知窗口(tertiary/20160 分钟)→ unknown 保守阻塞。"""

        snapshot = parse_appserver_response(
            load(fixture_path, "unknown_window.json"), thresholds=TH, captured_at=NOW
        )
        assert snapshot.ok
        unknowns = [w for w in snapshot.windows if w.window_kind == KIND_UNKNOWN]
        assert len(unknowns) == 1 and unknowns[0].bucket_key == "20160"
        assessment = assess_snapshot(snapshot, thresholds=TH)
        assert assessment.blocked and assessment.blocked_kind == KIND_UNKNOWN

    def test_no_minutes_inferred_both_windows(self, fixture_path):
        snapshot = parse_appserver_response(
            load(fixture_path, "no_minutes_inferred.json"), thresholds=TH, captured_at=NOW
        )
        kinds = windows_by_kind(snapshot)
        assert kinds[KIND_FIVE_HOUR].kind_inferred
        assert kinds[KIND_WEEKLY].kind_inferred

    @pytest.mark.parametrize("bad", [None, [], "string", 42, {"rateLimitsByLimitId": "not-a-dict"}])
    def test_unparseable_shapes_fail_closed(self, bad):
        snapshot = parse_appserver_response(bad, thresholds=TH, captured_at=NOW)
        assert snapshot.ok is False and snapshot.error
        assert assess_snapshot(snapshot, thresholds=TH).blocked


class TestJsonlShapes:
    def read(self, fixture_path, name):
        lines = fixture_path("jsonl", name).read_text(encoding="utf-8").splitlines()
        payload = None
        for line in lines:
            obj = json.loads(line)
            if obj.get("type") == "event_msg" and obj["payload"].get("type") == "token_count":
                payload = obj["payload"]["rate_limits"]
        return payload

    def test_snake_case_primary_secondary(self, fixture_path):
        """JSONL 实测形状(snake_case;evidence §3 真实样本的脱敏重构造)。"""

        payload = self.read(fixture_path, "token_count_5h_100.jsonl")
        snapshot = parse_jsonl_rate_limits(payload, thresholds=TH, captured_at=NOW)
        assert snapshot.ok and snapshot.source == "jsonl"
        kinds = windows_by_kind(snapshot)
        assert kinds[KIND_FIVE_HOUR].used_percent == 100.0
        assert kinds[KIND_FIVE_HOUR].is_exhausted  # S-01:五小时 100%
        assert kinds[KIND_WEEKLY].used_percent == 47.0
        assert snapshot.ordinary_usage_allowed is None  # JSONL 无该字段
        assessment = assess_snapshot(snapshot, thresholds=TH)
        assert assessment.blocked and assessment.blocking_keys == ("300",)

    def test_camel_case_tolerated(self, fixture_path):
        """D-QM-1 反向:JSONL 出现 camelCase 键同样可解析。"""

        payload = self.read(fixture_path, "token_count_camel.jsonl")
        snapshot = parse_jsonl_rate_limits(payload, thresholds=TH, captured_at=NOW)
        assert snapshot.ok
        kinds = windows_by_kind(snapshot)
        assert kinds[KIND_FIVE_HOUR].used_percent == 47.0

    def test_99_exhausted_same_path_as_100(self, fixture_path):
        payload = self.read(fixture_path, "token_count_5h_99.jsonl")
        snapshot = parse_jsonl_rate_limits(payload, thresholds=TH, captured_at=NOW)
        five = windows_by_kind(snapshot)[KIND_FIVE_HOUR]
        assert five.used_percent == 99.0 and five.is_exhausted

    def test_weekly_exhausted(self, fixture_path):
        payload = self.read(fixture_path, "token_count_weekly_100.jsonl")
        snapshot = parse_jsonl_rate_limits(payload, thresholds=TH, captured_at=NOW)
        weekly = windows_by_kind(snapshot)[KIND_WEEKLY]
        assert weekly.is_exhausted
        assessment = assess_snapshot(snapshot, thresholds=TH)
        assert assessment.blocking_keys == ("10080",)

    def test_dual_exhausted(self, fixture_path):
        payload = self.read(fixture_path, "token_count_dual_exhausted.jsonl")
        snapshot = parse_jsonl_rate_limits(payload, thresholds=TH, captured_at=NOW)
        assessment = assess_snapshot(snapshot, thresholds=TH)
        assert set(assessment.blocking_keys) == {"300", "10080"}

    def test_reached_type_recorded(self, fixture_path):
        payload = self.read(fixture_path, "token_count_5h_99.jsonl")
        snapshot = parse_jsonl_rate_limits(payload, thresholds=TH, captured_at=NOW)
        assert snapshot.rate_limit_reached_type == "rate_limit_reached"

    @pytest.mark.parametrize("bad", [None, "x", 42, []])
    def test_bad_payload_fail_closed(self, bad):
        snapshot = parse_jsonl_rate_limits(bad, thresholds=TH, captured_at=NOW)
        assert snapshot.ok is False and snapshot.error

    def test_normal_not_blocked(self, fixture_path):
        payload = self.read(fixture_path, "token_count_normal.jsonl")
        snapshot = parse_jsonl_rate_limits(payload, thresholds=TH, captured_at=NOW)
        assert not assess_snapshot(snapshot, thresholds=TH).blocked
