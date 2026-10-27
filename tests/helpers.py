"""P4 测试助手:rollout JSONL 事件构造(与 Fake 时钟起点对齐)。

时间基准 = conftest.fake_clock 起点 2026-01-01T00:00:00Z(1767225600),
事件时间用相对偏移秒构造,便于断言。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

BASE = datetime(2026, 1, 1, tzinfo=UTC)
T0 = 1767225600  # BASE 的 Unix 秒

FIVE_HOUR = 300
WEEKLY = 10080


def _iso(offset_sec: int) -> str:
    return (BASE + timedelta(seconds=offset_sec)).isoformat().replace("+00:00", "Z")


def event(offset_sec: int, type_: str, payload: dict[str, Any] | None = None) -> str:
    return json.dumps(
        {"timestamp": _iso(offset_sec), "type": type_, "payload": payload or {}},
        ensure_ascii=False,
    )


def session_meta(offset_sec: int = 0, *, cwd: str = "/tmp/project") -> str:
    return event(offset_sec, "session_meta", {"cwd": cwd})


def turn_context(offset_sec: int) -> str:
    return event(offset_sec, "turn_context", {"model": "test-model"})


def task_started(offset_sec: int, turn_id: str, *, trigger: str = "user") -> str:
    return event(
        offset_sec,
        "event_msg",
        {
            "type": "task_started",
            "turn_id": turn_id,
            "turn_attribution": {"turn_trigger": trigger},
        },
    )


def token_count(
    offset_sec: int,
    *,
    primary: dict[str, Any] | None = None,
    secondary: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    rate_limits: dict[str, Any] = {}
    if primary is not None:
        rate_limits["primary"] = primary
    if secondary is not None:
        rate_limits["secondary"] = secondary
    if extra:
        rate_limits.update(extra)
    return event(offset_sec, "event_msg", {"type": "token_count", "rate_limits": rate_limits})


def window(used_percent: float, resets_offset_sec: int, minutes: int = FIVE_HOUR) -> dict[str, Any]:
    """snake_case 窗口(JSONL 形态;resets 用相对偏移)。"""

    return {
        "used_percent": used_percent,
        "resets_at": T0 + resets_offset_sec,
        "window_minutes": minutes,
    }


def task_complete(offset_sec: int, turn_id: str, error: dict[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {"type": "task_complete", "turn_id": turn_id}
    if error is not None:
        payload["error"] = error
    return event(offset_sec, "event_msg", payload)


def usage_limit_error() -> dict[str, Any]:
    return {"codex_error_info": "usage_limit_exceeded"}


def error_variant(variant: str) -> dict[str, Any]:
    return {"codex_error_info": variant}


def user_message(offset_sec: int, text: str, item_id: str = "u1") -> str:
    return event(
        offset_sec,
        "response_item",
        {
            "type": "message",
            "role": "user",
            "id": item_id,
            "content": [{"type": "input_text", "text": text}],
        },
    )


def failed_turn(
    *,
    start: int,
    end: int,
    turn_id: str,
    primary: dict[str, Any] | None = None,
    secondary: dict[str, Any] | None = None,
    variant: str = "usage_limit_exceeded",
    sample_at: int | None = None,
    trigger: str = "user",
    extra: dict[str, Any] | None = None,
) -> list[str]:
    """一次完整失败轮次的事件序列。"""

    lines = [turn_context(start), task_started(start + 1, turn_id, trigger=trigger)]
    if primary is not None or secondary is not None:
        lines.append(
            token_count(
                sample_at if sample_at is not None else end - 30,
                primary=primary,
                secondary=secondary,
                extra=extra,
            )
        )
    lines.append(task_complete(end, turn_id, error_variant(variant)))
    return lines


def completed_turn(*, start: int, end: int, turn_id: str) -> list[str]:
    return [turn_context(start), task_started(start + 1, turn_id), task_complete(end, turn_id)]


def file_content(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"
