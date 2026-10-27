"""失败检测器:rollout JSONL 增量轮次跟踪与失败分类(P4-T2)。

数据源(P4 范围):rollout JSONL(thread_history 库主源随 P5-T4 接入,
本实现的分类表与证据结构对两源通用)。判定规则(recovery-state-machine §4.2
回退路径 + §4.3 细分):

- `task_complete.error.codex_error_info == "usage_limit_exceeded"`(camelCase
  变体 `usageLimitExceeded` 同收)→ 额度失败,按该轮次最近 token_count 样本
  细分窗口类;样本缺失/不可归因 → `quota_unknown`(manual_ack);
- 轮次未闭合(有 turn_context/task_started 无 task_complete)且轮内最近
  token_count 样本显示任一窗口 ≥ 耗尽阈值 → 额度失败(同上细分);
- 用户中断(aborted/interrupted)→ `aborted` → NEEDS_HUMAN(FR-08-A6);
- 终态类变体 → `terminal_error`;未知变体 → `unknown_error`(永不自动重试);
- 瞬态类 → 只观察不建失败(v1 不自动续);
- 干净 task_complete → 观察到任务完成(S-26,审计);不是失败。

纪律:
- 只基于完整行判定;撕裂尾行由 RolloutReader 不消费(下轮补读);
- 幂等由落库层按 turn 去重(FailureDao);本层在进程内靠游标天然不重放,
  重启后重建游标(全量重读)会重报,Dedup 兜底;
- 消息正文不落日志/证据(NFR-09):人工消息只留时间戳 + 首 32 字符。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codex_autopilot.codexif.discovery import find_rollout_chain
from codex_autopilot.codexif.rollout import RolloutError, RolloutEvent, RolloutReader
from codex_autopilot.quota.model import (
    KIND_FIVE_HOUR,
    KIND_UNKNOWN,
    KIND_WEEKLY,
    QuotaThresholds,
)
from codex_autopilot.quota.parser import parse_jsonl_rate_limits
from codex_autopilot.recovery.model import (
    KIND_ABORTED,
    KIND_QUOTA_CREDITS,
    KIND_QUOTA_FIVE_HOUR,
    KIND_QUOTA_MULTI,
    KIND_QUOTA_SPEND_CONTROL,
    KIND_QUOTA_UNKNOWN,
    KIND_QUOTA_WEEKLY,
    KIND_RATE_LIMIT_TRANSIENT,
    KIND_TERMINAL,
    KIND_TRANSIENT,
    KIND_UNKNOWN_ERROR,
    DetectedFailure,
    HumanMessage,
    SessionObservation,
    WindowEvidence,
)

USAGE_LIMIT_VARIANTS = frozenset({"usage_limit_exceeded", "usageLimitExceeded"})
ABORT_VARIANTS = frozenset({"aborted", "interrupted", "turn_interrupted"})
TRANSIENT_VARIANTS = frozenset(
    {
        "rate_limit_exceeded",
        "response_too_many_failed_attempts",
        "server_overloaded",
        "internal_server_error",
        "http_connection_failed",
        "timeout",
    }
)
TERMINAL_VARIANTS = frozenset(
    {"context_window_exceeded", "bad_request", "policy", "unauthorized", "invalid_request"}
)

_HUMAN_HEAD_CHARS = 32  # G3 证据摘要长度(不含全文)


@dataclass
class _OpenTurn:
    """进行中轮次的跟踪状态(分裂链跨文件延续)。"""

    turn_id: str | None = None
    started_ts: int | None = None
    task_started_seen: bool = False
    turn_trigger: str | None = None
    sample_payload: dict[str, Any] | None = None
    sample_ts: int | None = None
    start_offset: int = 0


@dataclass
class _SessionState:
    """单会话的增量读取器与轮次状态(内存态;重启重建,见模块 docstring)。"""

    readers: dict[Path, RolloutReader] = field(default_factory=dict)
    turn: _OpenTurn = field(default_factory=_OpenTurn)
    # 已判定过的轮次集合(防同轮次重复上报;进程内游标之外的二道防线)
    reported_turns: set[str] = field(default_factory=set)


def _error_variant(error: Any) -> str | None:
    """task_complete.error → 错误变体名(两套拼写兼容;无 error 返回 None)。"""

    if not isinstance(error, dict):
        return None
    for key in ("codex_error_info", "codexErrorInfo"):
        variant = error.get(key)
        if isinstance(variant, str) and variant:
            return variant
    return None


def classify_variant(variant: str) -> str:
    """错误变体 → 失败类别(recovery-state-machine §4.2 分类表)。"""

    if variant in USAGE_LIMIT_VARIANTS:
        return "usage_limit"
    if variant in ABORT_VARIANTS:
        return KIND_ABORTED
    if variant.startswith("response_stream"):
        return KIND_TRANSIENT
    if variant in TRANSIENT_VARIANTS:
        if variant == "rate_limit_exceeded" or variant == "response_too_many_failed_attempts":
            return KIND_RATE_LIMIT_TRANSIENT
        return KIND_TRANSIENT
    if variant in TERMINAL_VARIANTS:
        return KIND_TERMINAL
    return KIND_UNKNOWN_ERROR  # unknown 永不自动重试


class FailureDetector:
    """按会话增量消费 rollout 事件流,产出 SessionObservation。

    用法:每 tick 对每个受管会话调用 `observe(codex_home, uuid)`;实例横跨
    tick 存活(游标与轮次状态在内存)。调度器负责异常隔离与落库。
    """

    def __init__(self, thresholds: QuotaThresholds):
        self._thresholds = thresholds
        self._sessions: dict[str, _SessionState] = {}

    # -- 对外 -------------------------------------------------------------

    def observe(self, codex_home: Path, session_uuid: str, *, now: int) -> SessionObservation:
        """读取该会话链上的新增事件并归类(不落库、不改状态机)。"""

        state = self._sessions.setdefault(session_uuid, _SessionState())
        failures: list[DetectedFailure] = []
        completed: list[dict[str, object]] = []
        human_messages: list[HumanMessage] = []
        transient_events: list[dict[str, object]] = []
        read_errors: list[str] = []
        last_event_at: int | None = None

        for session_file in find_rollout_chain(Path(codex_home), session_uuid):
            reader = state.readers.get(session_file.path)
            if reader is None:
                reader = RolloutReader(session_file.path)
                state.readers[session_file.path] = reader
            try:
                events = reader.read_new_events()
            except RolloutError as exc:
                read_errors.append(f"{session_file.path}: {exc}")
                continue
            for event in events:
                if event.timestamp is not None:
                    last_event_at = event.timestamp
                self._consume(
                    event,
                    state=state,
                    failures=failures,
                    completed=completed,
                    human_messages=human_messages,
                    transient_events=transient_events,
                    now=now,
                )

        latest = (state.turn.sample_payload, state.turn.sample_ts)
        return SessionObservation(
            session_uuid=session_uuid,
            failures=tuple(failures),
            task_completed=tuple(completed),
            human_messages=tuple(human_messages),
            transient_events=tuple(transient_events),
            latest_rate_limits=latest if state.turn.sample_payload is not None else None,
            last_event_at=last_event_at,
            has_open_turn=state.turn.task_started_seen or state.turn.turn_id is not None,
            read_errors=tuple(read_errors),
        )

    def reset_session(self, session_uuid: str) -> None:
        """丢弃该会话的游标与轮次状态(下轮从 0 重建;测试与失效场景用)。"""

        self._sessions.pop(session_uuid, None)

    # -- 事件处理 ---------------------------------------------------------

    def _consume(
        self,
        event: RolloutEvent,
        *,
        state: _SessionState,
        failures: list[DetectedFailure],
        completed: list[dict[str, object]],
        human_messages: list[HumanMessage],
        transient_events: list[dict[str, object]],
        now: int,
    ) -> None:
        if event.type == "turn_context":
            # 新轮次开始;前一轮未闭合则隐式让位(其判定已在先前事件完成)
            state.turn = _OpenTurn(start_offset=event.offset)
            return
        if event.type == "response_item":
            if event.is_user_message:
                text = self._message_head(event.payload)
                human_messages.append(
                    HumanMessage(
                        timestamp=event.timestamp,
                        head=text,
                        turn_id=state.turn.turn_id,
                    )
                )
            return
        if event.type != "event_msg":
            return
        payload_type = event.payload_type
        if payload_type == "task_started":
            state.turn.task_started_seen = True
            turn_id = event.payload.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                state.turn.turn_id = turn_id
            attribution = event.payload.get("turn_attribution")
            if isinstance(attribution, dict) and isinstance(attribution.get("turn_trigger"), str):
                state.turn.turn_trigger = attribution["turn_trigger"]
            if state.turn.started_ts is None:
                state.turn.started_ts = event.timestamp
            return
        if payload_type == "token_count":
            sample = event.rate_limits_payload()
            if sample is not None:
                state.turn.sample_payload = sample
                state.turn.sample_ts = event.timestamp
            return
        if payload_type == "task_complete":
            self._close_turn(
                event,
                state=state,
                failures=failures,
                completed=completed,
                transient_events=transient_events,
                now=now,
            )
            return
        # 其余 event_msg(item_completed 等):不参与判定

    def _close_turn(
        self,
        event: RolloutEvent,
        *,
        state: _SessionState,
        failures: list[DetectedFailure],
        completed: list[dict[str, object]],
        transient_events: list[dict[str, object]],
        now: int,
    ) -> None:
        turn = state.turn
        payload_turn_id = event.payload.get("turn_id")
        turn_id = payload_turn_id if isinstance(payload_turn_id, str) and payload_turn_id else None
        if turn_id is None:
            turn_id = turn.turn_id
        if turn_id is None:
            turn_id = (
                f"turn@{turn.started_ts if turn.started_ts is not None else turn.start_offset}"
            )

        error = event.payload.get("error")
        variant = _error_variant(error)
        if variant is None:
            completed.append({"turn_id": turn_id, "timestamp": event.timestamp, "source": "jsonl"})
            state.reported_turns.add(turn_id)
            state.turn = _OpenTurn()
            return

        category = classify_variant(variant)
        if category == "usage_limit":
            failures.append(
                self._quota_failure(
                    turn_id=turn_id,
                    detected_at=now,
                    sample_payload=turn.sample_payload,
                    sample_ts=turn.sample_ts,
                    error_variant=variant,
                    turn_ts=event.timestamp,
                    include_hint=True,  # 错误事件已证实额度归因 → 90-98 hint 窗口也计入 blocking
                )
            )
        elif category in (KIND_ABORTED, KIND_TERMINAL, KIND_UNKNOWN_ERROR):
            failures.append(
                DetectedFailure(
                    turn_id=turn_id,
                    kind=category,
                    detected_at=now,
                    evidence={
                        "source": "jsonl",
                        "error_variant": variant,
                        "task_completed_at": event.timestamp,
                        "turn_started_at": turn.started_ts,
                    },
                )
            )
        elif category in (KIND_RATE_LIMIT_TRANSIENT, KIND_TRANSIENT):
            # 瞬态类:不建失败(v1 只记录观察;调度器转审计,不自动续)
            transient_events.append(
                {
                    "turn_id": turn_id,
                    "category": category,
                    "error_variant": variant,
                    "timestamp": event.timestamp,
                }
            )
        state.reported_turns.add(turn_id)
        state.turn = _OpenTurn()

    # -- 额度失败细分(recovery §4.3) ------------------------------------

    def _quota_failure(
        self,
        *,
        turn_id: str,
        detected_at: int,
        sample_payload: dict[str, Any] | None,
        sample_ts: int | None,
        error_variant: str | None,
        turn_ts: int | None,
        include_hint: bool,
    ) -> DetectedFailure:
        evidence: dict[str, object] = {
            "source": "jsonl",
            "error_variant": error_variant,
            "task_completed_at": turn_ts,
        }
        if sample_payload is None:
            # 轮次确未完成 + 无法排除额度原因 → quota_unknown(manual_ack)
            evidence["reason"] = "no_token_count_sample"
            return DetectedFailure(
                turn_id=turn_id,
                kind=KIND_QUOTA_UNKNOWN,
                detected_at=detected_at,
                evidence=evidence,
            )

        snapshot = parse_jsonl_rate_limits(
            sample_payload, thresholds=self._thresholds, captured_at=sample_ts or detected_at
        )
        evidence["snapshot_captured_at"] = sample_ts
        if not snapshot.ok:
            evidence["reason"] = "sample_unparseable"
            return DetectedFailure(
                turn_id=turn_id,
                kind=KIND_QUOTA_UNKNOWN,
                detected_at=detected_at,
                evidence=evidence,
            )
        if snapshot.spend_control_reached:
            evidence["reason"] = "spend_control_reached"
            return DetectedFailure(
                turn_id=turn_id,
                kind=KIND_QUOTA_SPEND_CONTROL,
                detected_at=detected_at,
                evidence=evidence,
            )
        reached = snapshot.rate_limit_reached_type
        if reached and "credits_depleted" in reached:
            evidence["reason"] = "credits_reached"
            return DetectedFailure(
                turn_id=turn_id, kind=KIND_QUOTA_CREDITS, detected_at=detected_at, evidence=evidence
            )

        selected = [w for w in snapshot.windows if w.is_exhausted or (include_hint and w.hint)]
        if not selected:
            # 错误说 usage_limit 但窗口全部低于 hint 带:无法归因窗口 → unknown
            evidence["reason"] = "no_blocking_window"
            return DetectedFailure(
                turn_id=turn_id,
                kind=KIND_QUOTA_UNKNOWN,
                detected_at=detected_at,
                evidence=evidence,
            )
        kinds = {w.window_kind for w in selected}
        if KIND_UNKNOWN in kinds or any(w.used_percent is None for w in selected):
            # 不可评估窗口参与 blocking → 整体按 unknown(G6 恒不通过)
            evidence["reason"] = "unverifiable_window"
            return DetectedFailure(
                turn_id=turn_id,
                kind=KIND_QUOTA_UNKNOWN,
                detected_at=detected_at,
                evidence=evidence,
            )
        if kinds == {KIND_FIVE_HOUR}:
            kind = KIND_QUOTA_FIVE_HOUR
        elif kinds == {KIND_WEEKLY}:
            kind = KIND_QUOTA_WEEKLY
        else:
            kind = KIND_QUOTA_MULTI
        evidence["windows"] = [
            {
                "bucket_key": w.bucket_key,
                "window_kind": w.window_kind,
                "used_percent": w.used_percent,
                "resets_at": w.resets_at,
                "window_minutes": w.window_minutes,
            }
            for w in selected
        ]
        return DetectedFailure(
            turn_id=turn_id,
            kind=kind,
            detected_at=detected_at,
            blocking_window_keys=tuple(dict.fromkeys(w.bucket_key for w in selected)),
            windows=tuple(
                WindowEvidence(
                    bucket_key=w.bucket_key,
                    window_kind=w.window_kind,
                    used_percent=w.used_percent,
                    resets_at=w.resets_at,
                    window_minutes=w.window_minutes,
                )
                for w in selected
            ),
            evidence=evidence,
        )

    def unclosed_turn_failure(self, session_uuid: str, *, now: int) -> DetectedFailure | None:
        """轮次未闭合 + 轮内最近样本显示窗口 ≥ 耗尽阈值 → 额度失败(否则 None)。

        供调度器每 tick 轮询(S-01 检测侧);阈值判定不含 hint 带(无错误
        事件证实归因时更保守)。样本取轮内最近一次 token_count。
        """

        state = self._sessions.get(session_uuid)
        if state is None or state.turn.sample_payload is None:
            return None
        if not (state.turn.task_started_seen or state.turn.turn_id is not None):
            return None
        turn = state.turn
        # 无错误事件证实归因 ⇒ 必须有任一窗口 ≥ 耗尽阈值才成立(§4.2 回退路径:
        # "同期 token_count 快照显示窗口 ≥ 阈值";hint 带不算,unknown 窗口算)
        snapshot = parse_jsonl_rate_limits(
            turn.sample_payload, thresholds=self._thresholds, captured_at=turn.sample_ts or now
        )
        if not snapshot.ok or not any(w.is_exhausted for w in snapshot.windows):
            return None
        turn_id = (
            turn.turn_id
            or f"turn@{turn.started_ts if turn.started_ts is not None else turn.start_offset}"
        )
        return self._quota_failure(
            turn_id=turn_id,
            detected_at=now,
            sample_payload=turn.sample_payload,
            sample_ts=turn.sample_ts,
            error_variant=None,
            turn_ts=turn.started_ts,
            include_hint=False,
        )

    # -- 内部 -------------------------------------------------------------

    def _message_head(self, payload: dict[str, Any]) -> str:
        content = payload.get("content")
        text = ""
        if isinstance(content, list):
            parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            text = " ".join(parts)
        elif isinstance(content, str):
            text = content
        return " ".join(text.split())[:_HUMAN_HEAD_CHARS]
