"""P4-T2 失败检测器单元测试:分类表、轮次跟踪、增量游标、幂等观察。

对应设计:recovery-state-machine §4.2/§4.3;S-01/02/03/04/05/20/26/28 检测侧。
"""

from __future__ import annotations

from pathlib import Path

from codex_autopilot.quota.model import QuotaThresholds
from codex_autopilot.recovery.detector import FailureDetector, classify_variant
from codex_autopilot.recovery.model import (
    KIND_ABORTED,
    KIND_QUOTA_FIVE_HOUR,
    KIND_QUOTA_MULTI,
    KIND_QUOTA_UNKNOWN,
    KIND_QUOTA_WEEKLY,
    KIND_RATE_LIMIT_TRANSIENT,
    KIND_TERMINAL,
    KIND_TRANSIENT,
    KIND_UNKNOWN_ERROR,
)
from tests import helpers as h

UUID = "01234567-89ab-cdef-0123-456789abcdef"
OTHER = "fedcba98-7654-3210-fedc-ba9876543210"


def make_detector() -> FailureDetector:
    return FailureDetector(QuotaThresholds())


def write(codex_home: Path, content: str, *, ts: str = "2026-01-01T00-00-00") -> Path:
    directory = codex_home / "sessions" / "2026" / "01" / "01"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-{ts}-{UUID}.jsonl"
    path.write_text(content, encoding="utf-8")
    return path


class TestClassifyVariant:
    def test_usage_limit_both_spellings(self):
        assert classify_variant("usage_limit_exceeded") == "usage_limit"
        assert classify_variant("usageLimitExceeded") == "usage_limit"

    def test_abort_variants(self):
        assert classify_variant("aborted") == KIND_ABORTED
        assert classify_variant("interrupted") == KIND_ABORTED

    def test_transient_variants(self):
        assert classify_variant("rate_limit_exceeded") == KIND_RATE_LIMIT_TRANSIENT
        assert classify_variant("response_too_many_failed_attempts") == KIND_RATE_LIMIT_TRANSIENT
        assert classify_variant("server_overloaded") == KIND_TRANSIENT
        assert classify_variant("internal_server_error") == KIND_TRANSIENT
        assert classify_variant("http_connection_failed") == KIND_TRANSIENT
        assert classify_variant("response_stream_error") == KIND_TRANSIENT

    def test_terminal_and_unknown(self):
        assert classify_variant("context_window_exceeded") == KIND_TERMINAL
        assert classify_variant("unauthorized") == KIND_TERMINAL
        assert classify_variant("some_new_variant") == KIND_UNKNOWN_ERROR


class TestQuotaFailureClassification:
    def test_five_hour_exhausted(self, tmp_path):
        content = h.file_content(
            [
                h.session_meta(),
                *h.failed_turn(
                    start=60,
                    end=120,
                    turn_id="t-1",
                    primary=h.window(100, 18000),
                    secondary=h.window(40, 700000, minutes=h.WEEKLY),
                ),
            ]
        )
        home = tmp_path / "home"
        write(home, content)
        obs = make_detector().observe(home, UUID, now=h.T0 + 200)
        assert len(obs.failures) == 1
        failure = obs.failures[0]
        assert failure.kind == KIND_QUOTA_FIVE_HOUR
        assert failure.blocking_window_keys == ("300",)
        assert failure.windows[0].resets_at == h.T0 + 18000
        assert failure.evidence["error_variant"] == "usage_limit_exceeded"

    def test_weekly_only(self, tmp_path):
        content = h.file_content(
            [
                h.session_meta(),
                *h.failed_turn(
                    start=60,
                    end=120,
                    turn_id="t-1",
                    primary=h.window(30, 18000),
                    secondary=h.window(100, 700000, minutes=h.WEEKLY),
                ),
            ]
        )
        home = tmp_path / "home"
        write(home, content)
        obs = make_detector().observe(home, UUID, now=h.T0 + 200)
        assert obs.failures[0].kind == KIND_QUOTA_WEEKLY
        assert obs.failures[0].blocking_window_keys == ("10080",)

    def test_dual_window_multi(self, tmp_path):
        content = h.file_content(
            [
                h.session_meta(),
                *h.failed_turn(
                    start=60,
                    end=120,
                    turn_id="t-1",
                    primary=h.window(100, 18000),
                    secondary=h.window(99, 700000, minutes=h.WEEKLY),
                ),
            ]
        )
        home = tmp_path / "home"
        write(home, content)
        obs = make_detector().observe(home, UUID, now=h.T0 + 200)
        assert obs.failures[0].kind == KIND_QUOTA_MULTI
        assert obs.failures[0].blocking_window_keys == ("300", "10080")

    def test_hint_window_counts_when_error_confirms_usage_limit(self, tmp_path):
        """90-98% hint:错误事件证实额度归因时计入 blocking(recovery §4.3)。"""

        content = h.file_content(
            [
                h.session_meta(),
                *h.failed_turn(start=60, end=120, turn_id="t-1", primary=h.window(98, 18000)),
            ]
        )
        home = tmp_path / "home"
        write(home, content)
        obs = make_detector().observe(home, UUID, now=h.T0 + 200)
        assert obs.failures[0].kind == KIND_QUOTA_FIVE_HOUR

    def test_no_sample_is_quota_unknown(self, tmp_path):
        # 无 token_count 样本:无法归因窗口 → quota_unknown
        lines = [h.session_meta(), *h.failed_turn(start=60, end=120, turn_id="t-1")]
        content = h.file_content(lines)
        home = tmp_path / "home"
        write(home, content)
        obs = make_detector().observe(home, UUID, now=h.T0 + 200)
        assert obs.failures[0].kind == KIND_QUOTA_UNKNOWN
        assert obs.failures[0].evidence["reason"] == "no_token_count_sample"

    def test_spend_control_kind(self, tmp_path):
        content = h.file_content(
            [
                h.session_meta(),
                *h.failed_turn(
                    start=60,
                    end=120,
                    turn_id="t-1",
                    primary=h.window(100, 18000),
                    extra={"spend_control_reached": True},
                ),
            ]
        )
        home = tmp_path / "home"
        write(home, content)
        obs = make_detector().observe(home, UUID, now=h.T0 + 200)
        assert obs.failures[0].kind == "quota_spend_control"

    def test_credits_kind(self, tmp_path):
        content = h.file_content(
            [
                h.session_meta(),
                *h.failed_turn(
                    start=60,
                    end=120,
                    turn_id="t-1",
                    primary=h.window(100, 18000),
                    extra={"rate_limit_reached_type": "workspace_owner_credits_depleted"},
                ),
            ]
        )
        home = tmp_path / "home"
        write(home, content)
        obs = make_detector().observe(home, UUID, now=h.T0 + 200)
        assert obs.failures[0].kind == "quota_credits"


class TestNonQuotaKinds:
    def test_aborted_needs_human_kind(self, tmp_path):
        content = h.file_content(
            [h.session_meta(), *h.failed_turn(start=60, end=120, turn_id="t-1", variant="aborted")]
        )
        home = tmp_path / "home"
        write(home, content)
        obs = make_detector().observe(home, UUID, now=h.T0 + 200)
        assert obs.failures[0].kind == KIND_ABORTED

    def test_terminal_kind(self, tmp_path):
        content = h.file_content(
            [h.session_meta(), *h.failed_turn(start=60, end=120, turn_id="t-1", variant="policy")]
        )
        home = tmp_path / "home"
        write(home, content)
        obs = make_detector().observe(home, UUID, now=h.T0 + 200)
        assert obs.failures[0].kind == KIND_TERMINAL

    def test_unknown_variant_kind(self, tmp_path):
        content = h.file_content(
            [
                h.session_meta(),
                *h.failed_turn(start=60, end=120, turn_id="t-1", variant="brand_new_error"),
            ]
        )
        home = tmp_path / "home"
        write(home, content)
        obs = make_detector().observe(home, UUID, now=h.T0 + 200)
        assert obs.failures[0].kind == KIND_UNKNOWN_ERROR

    def test_transient_records_observation_not_failure(self, tmp_path):
        content = h.file_content(
            [
                h.session_meta(),
                *h.failed_turn(start=60, end=120, turn_id="t-1", variant="server_overloaded"),
            ]
        )
        home = tmp_path / "home"
        write(home, content)
        obs = make_detector().observe(home, UUID, now=h.T0 + 200)
        assert obs.failures == ()
        assert len(obs.transient_events) == 1
        assert obs.transient_events[0]["category"] == KIND_TRANSIENT

    def test_task_complete_without_error_is_completion(self, tmp_path):
        content = h.file_content(
            [h.session_meta(), *h.completed_turn(start=60, end=120, turn_id="t-1")]
        )
        home = tmp_path / "home"
        write(home, content)
        obs = make_detector().observe(home, UUID, now=h.T0 + 200)
        assert obs.failures == ()
        assert len(obs.task_completed) == 1
        assert obs.task_completed[0]["turn_id"] == "t-1"


class TestIncrementalCursor:
    def test_second_observe_reads_only_new_events(self, tmp_path):
        home = tmp_path / "home"
        path = write(
            home,
            h.file_content([h.session_meta(), *h.completed_turn(start=60, end=120, turn_id="t-1")]),
        )
        detector = make_detector()
        first = detector.observe(home, UUID, now=h.T0 + 200)
        assert len(first.task_completed) == 1

        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                h.file_content(
                    h.failed_turn(start=200, end=300, turn_id="t-2", primary=h.window(100, 18000))
                )
            )
        second = detector.observe(home, UUID, now=h.T0 + 400)
        assert len(second.task_completed) == 0  # 已消费
        assert len(second.failures) == 1
        assert second.failures[0].turn_id == "t-2"

    def test_same_turn_not_re_reported_within_detector(self, tmp_path):
        """同轮次闭合后,后续 observe 不重复上报(游标推进)。"""

        home = tmp_path / "home"
        write(
            home,
            h.file_content(
                [
                    h.session_meta(),
                    *h.failed_turn(start=60, end=120, turn_id="t-1", primary=h.window(100, 18000)),
                ]
            ),
        )
        detector = make_detector()
        assert len(detector.observe(home, UUID, now=h.T0 + 200).failures) == 1
        assert detector.observe(home, UUID, now=h.T0 + 300).failures == ()

    def test_split_chain_events_continue_across_files(self, tmp_path):
        """分裂链:续链文件延续事件流(轮次可跨文件)。"""

        home = tmp_path / "home"
        directory = home / "sessions" / "2026" / "01" / "01"
        directory.mkdir(parents=True)
        suffix = OTHER
        first = directory / f"rollout-2026-01-01T00-00-00-{UUID}.jsonl"
        first.write_text(
            h.file_content([h.session_meta(), h.turn_context(60), h.task_started(61, "t-1")]),
            encoding="utf-8",
        )
        second = directory / f"rollout-2026-01-01T01-00-00-{UUID}_{suffix}.jsonl"
        second.write_text(
            h.file_content(
                [
                    h.token_count(3600, primary=h.window(100, 18000)),
                    h.task_complete(3660, "t-1", h.usage_limit_error()),
                ]
            ),
            encoding="utf-8",
        )
        obs = make_detector().observe(home, UUID, now=h.T0 + 4000)
        assert len(obs.failures) == 1
        assert obs.failures[0].kind == KIND_QUOTA_FIVE_HOUR
        assert obs.failures[0].turn_id == "t-1"


class TestUnclosedTurn:
    def test_unclosed_turn_with_exhausted_sample_is_failure(self, tmp_path):
        home = tmp_path / "home"
        write(
            home,
            h.file_content(
                [
                    h.session_meta(),
                    h.turn_context(60),
                    h.task_started(61, "t-open"),
                    h.token_count(90, primary=h.window(100, 18000)),
                ]
            ),
        )
        detector = make_detector()
        obs = detector.observe(home, UUID, now=h.T0 + 200)
        assert obs.failures == ()  # 未闭合轮次由 unclosed_turn_failure 轮询
        assert obs.has_open_turn
        failure = detector.unclosed_turn_failure(UUID, now=h.T0 + 200)
        assert failure is not None and failure.kind == KIND_QUOTA_FIVE_HOUR

    def test_unclosed_turn_hint_only_not_failure(self, tmp_path):
        """无错误事件证实时,90-98 hint 不构成失败(更保守)。"""

        home = tmp_path / "home"
        write(
            home,
            h.file_content(
                [
                    h.session_meta(),
                    h.turn_context(60),
                    h.task_started(61, "t-open"),
                    h.token_count(90, primary=h.window(97, 18000)),
                ]
            ),
        )
        detector = make_detector()
        detector.observe(home, UUID, now=h.T0 + 200)
        assert detector.unclosed_turn_failure(UUID, now=h.T0 + 200) is None

    def test_unclosed_turn_without_sample_is_none(self, tmp_path):
        home = tmp_path / "home"
        write(home, h.file_content([h.session_meta(), h.turn_context(60), h.task_started(61, "t")]))
        detector = make_detector()
        detector.observe(home, UUID, now=h.T0 + 200)
        assert detector.unclosed_turn_failure(UUID, now=h.T0 + 200) is None

    def test_running_turn_marks_open(self, tmp_path):
        """S-28 检测侧:未闭合轮次 → has_open_turn(投递 G7 将延后)。"""

        home = tmp_path / "home"
        write(
            home,
            h.file_content([h.session_meta(), h.turn_context(60), h.task_started(61, "t")]),
        )
        obs = make_detector().observe(home, UUID, now=h.T0 + 100)
        assert obs.has_open_turn


class TestHumanMessages:
    def test_user_message_observed_with_head_only(self, tmp_path):
        home = tmp_path / "home"
        write(
            home,
            h.file_content(
                [h.session_meta(), h.user_message(30, "please continue the task " + "x" * 100)]
            ),
        )
        obs = make_detector().observe(home, UUID, now=h.T0 + 100)
        assert len(obs.human_messages) == 1
        message = obs.human_messages[0]
        assert message.timestamp == h.T0 + 30
        assert len(message.head) <= 32  # 不落全文(NFR-09)
        assert message.head.startswith("please continue")

    def test_assistant_messages_ignored(self, tmp_path):
        home = tmp_path / "home"
        write(
            home,
            h.file_content(
                [
                    h.session_meta(),
                    h.event(
                        30,
                        "response_item",
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "assistant text"}],
                        },
                    ),
                ]
            ),
        )
        obs = make_detector().observe(home, UUID, now=h.T0 + 100)
        assert obs.human_messages == ()
