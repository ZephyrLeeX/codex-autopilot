"""P3-T1 测试:rollout 增量解析(T-PARSE-01/02:S-23 撕裂行、游标、事件模型)。"""

from __future__ import annotations

import json
import logging

import pytest

from codex_autopilot.codexif.rollout import (
    RolloutError,
    RolloutReader,
    latest_rate_limits_sample,
    parse_event_timestamp,
    parse_line,
    read_file_events,
)

TOKEN_COUNT_LINE = json.dumps(
    {
        "timestamp": "2026-01-01T00:02:00Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {"input_tokens": 1}},
            "rate_limits": {
                "primary": {"resets_at": 1767243600, "used_percent": 47.0, "window_minutes": 300},
                "secondary": {
                    "resets_at": 1767657600,
                    "used_percent": 12.0,
                    "window_minutes": 10080,
                },
            },
        },
    }
)
META_LINE = json.dumps(
    {
        "timestamp": "2026-01-01T00:00:01Z",
        "type": "session_meta",
        "payload": {"id": "01234567-89ab-cdef-0123-456789abcdef", "cwd": "/tmp/project"},
    }
)


class TestParseLine:
    def test_valid_event(self):
        event = parse_line(TOKEN_COUNT_LINE)
        assert event is not None
        assert event.type == "event_msg"
        assert event.payload_type == "token_count"
        assert event.is_token_count
        assert event.timestamp == 1767225720  # 2026-01-01T00:02:00Z
        limits = event.rate_limits_payload()
        assert limits is not None and "primary" in limits

    def test_user_message_detection(self):
        line = json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": "placeholder"},
            }
        )
        event = parse_line(line)
        assert event is not None and event.is_user_message
        assert parse_line(META_LINE).is_user_message is False

    def test_bad_lines_return_none(self):
        assert parse_line("not json") is None
        assert parse_line("42") is None
        assert parse_line('["array","not","object"]') is None
        assert parse_line("") is None
        assert parse_line('{"type":"x"}') is None  # 缺 payload
        assert parse_line('{"timestamp":"2026-01-01T00:00:00Z","type":"event_msg"}') is None

    def test_unknown_top_level_type_tolerated(self):
        line = json.dumps(
            {"timestamp": "2026-01-01T00:00:00Z", "type": "world_state", "payload": {"x": 1}}
        )
        event = parse_line(line)
        assert event is not None and event.type == "world_state"


class TestTimestamps:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-01-01T00:00:00Z", 1767225600),
            ("2026-01-01T08:00:00+08:00", 1767225600),
            ("2026-01-01T00:00:00.123456Z", 1767225600),
            ("2026-01-01T00:00:00", 1767225600),  # naive 视为 UTC
        ],
    )
    def test_formats(self, raw, expected):
        assert parse_event_timestamp(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "yesterday", 12345, "2026-13-01T00:00:00Z"])
    def test_invalid(self, raw):
        assert parse_event_timestamp(raw) is None


class TestIncrementalReading:
    def test_first_read_then_incremental(self, tmp_path):
        path = tmp_path / "rollout.jsonl"
        path.write_text(META_LINE + "\n", encoding="utf-8")
        reader = RolloutReader(path)
        first = reader.read_new_events()
        assert len(first) == 1
        assert reader.read_new_events() == []  # 无新增
        with path.open("a", encoding="utf-8") as handle:
            handle.write(TOKEN_COUNT_LINE + "\n")
        second = reader.read_new_events()
        assert len(second) == 1 and second[0].is_token_count
        assert reader.offset == path.stat().st_size

    def test_torn_tail_not_consumed_then_completed(self, tmp_path, caplog):
        """S-23/FR-08-A8:撕裂尾行不消费;写完后下一次读取补齐。"""

        path = tmp_path / "rollout.jsonl"
        path.write_text(META_LINE + "\n" + TOKEN_COUNT_LINE[:-15], encoding="utf-8")  # 无尾换行
        reader = RolloutReader(path)
        with caplog.at_level(logging.WARNING):
            events = reader.read_new_events()
        assert len(events) == 1  # 只有完整行
        assert reader.stats.torn_tail_lines == 1
        assert any("incomplete" in message for message in caplog.messages)
        assert reader.read_new_events() == []  # 撕裂行仍不产出事件

        with path.open("a", encoding="utf-8") as handle:  # 写完该行
            handle.write(TOKEN_COUNT_LINE[-15:] + "\n")
        events = reader.read_new_events()
        assert len(events) == 1 and events[0].is_token_count  # 补读成功

    def test_bad_middle_line_skipped(self, tmp_path, caplog):
        path = tmp_path / "rollout.jsonl"
        content = META_LINE + "\nnot-json{{{\n" + TOKEN_COUNT_LINE + "\n"
        path.write_text(content, encoding="utf-8")
        reader = RolloutReader(path)
        with caplog.at_level(logging.WARNING):
            events = reader.read_new_events()
        assert [e.type for e in events] == ["session_meta", "event_msg"]
        assert reader.stats.bad_lines == 1
        assert any("not a valid event" in message for message in caplog.messages)

    def test_empty_file(self, tmp_path):
        path = tmp_path / "rollout.jsonl"
        path.write_text("", encoding="utf-8")
        assert RolloutReader(path).read_new_events() == []

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(RolloutError, match="cannot stat"):
            RolloutReader(tmp_path / "absent.jsonl").read_new_events()

    def test_unreadable_file_raises(self, tmp_path):
        path = tmp_path / "rollout.jsonl"
        path.write_text(META_LINE + "\n", encoding="utf-8")
        path.chmod(0o000)
        try:
            with pytest.raises(RolloutError):
                RolloutReader(path).read_new_events()
        finally:
            path.chmod(0o644)

    def test_shrunk_file_rereads_from_start(self, tmp_path, caplog):
        path = tmp_path / "rollout.jsonl"
        path.write_text(META_LINE + "\n" + TOKEN_COUNT_LINE + "\n", encoding="utf-8")
        reader = RolloutReader(path)
        assert len(reader.read_new_events()) == 2
        with caplog.at_level(logging.WARNING):
            path.write_text(META_LINE + "\n", encoding="utf-8")  # 截断/轮转
            events = reader.read_new_events()
        assert len(events) == 1
        assert any("shrank" in message for message in caplog.messages)

    def test_fixture_files_parse(self, fixture_path):
        for name in ("token_count_normal.jsonl", "token_count_5h_100.jsonl"):
            events = read_file_events(fixture_path("jsonl", name))
            assert events, name
            assert all(
                e.type
                in {
                    "session_meta",
                    "turn_context",
                    "response_item",
                    "event_msg",
                    "token_usage_record",
                    "world_state",
                }
                for e in events
            )


class TestRateLimitsExtraction:
    def test_latest_sample_wins(self):
        def tc(turn, used):
            return parse_line(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "rate_limits": {
                                "primary": {"used_percent": used, "window_minutes": 300}
                            },
                        },
                    }
                )
            )

        events = [e for e in (tc(1, 10.0), tc(2, 20.0), tc(3, 30.0)) if e]
        sample = latest_rate_limits_sample(events)
        assert sample is not None
        payload, ts, _line_no = sample
        assert payload["primary"]["used_percent"] == 30.0
        assert ts == 1767225600

    def test_no_token_count_returns_none(self, fixture_path):
        events = read_file_events(fixture_path("jsonl", "no_token_count.jsonl"))
        assert latest_rate_limits_sample(events) is None

    def test_non_dict_rate_limits_ignored(self):
        event = parse_line(
            json.dumps(
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count", "rate_limits": "garbage"},
                }
            )
        )
        assert event is not None
        assert event.rate_limits_payload() is None


class TestReadonlyGuarantee:
    def test_reader_never_writes(self, tmp_path):
        content = META_LINE + "\n" + TOKEN_COUNT_LINE + "\n"
        path = tmp_path / "rollout.jsonl"
        path.write_text(content, encoding="utf-8")
        before = path.read_bytes()
        RolloutReader(path).read_new_events()
        RolloutReader(path).read_new_events()
        assert path.read_bytes() == before  # 原始文件字节不变(红线)
