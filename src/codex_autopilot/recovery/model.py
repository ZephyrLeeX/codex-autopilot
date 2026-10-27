"""恢复域模型:失败类别与检测产物(recovery-state-machine §4)。

kind 语义与处置:
- `quota_*`:额度失败,按阻塞窗口/标志细分;window 类可自动恢复
  (策略 window_reset),credits/spend_control/unknown 只能人工或官方
  解除(策略 manual_ack,不自动投递);
- `aborted`:用户中断(FR-08-A6)→ NEEDS_HUMAN,绝不自动重启;
- `terminal_error`:重试无意义的终态类错误 → NEEDS_HUMAN;
- `unknown_error`:未知错误变体("unknown 永不自动重试")→ NEEDS_HUMAN;
- `rate_limit_transient` / `transient`:瞬态类,v1 只记录与审计,
  不建失败、不自动续(未来扩展)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 失败类别(persistence §3 failures.kind;注释值为权威枚举)
KIND_QUOTA_FIVE_HOUR = "quota_five_hour"
KIND_QUOTA_WEEKLY = "quota_weekly"
KIND_QUOTA_MULTI = "quota_multi"
KIND_QUOTA_CREDITS = "quota_credits"
KIND_QUOTA_SPEND_CONTROL = "quota_spend_control"
KIND_QUOTA_UNKNOWN = "quota_unknown"
KIND_ABORTED = "aborted"
KIND_TERMINAL = "terminal_error"
KIND_UNKNOWN_ERROR = "unknown_error"

# 瞬态类观察类别(不建失败,仅审计;v1 不自动续,未来扩展)
KIND_RATE_LIMIT_TRANSIENT = "rate_limit_transient"
KIND_TRANSIENT = "transient"

QUOTA_WINDOW_KINDS = frozenset({KIND_QUOTA_FIVE_HOUR, KIND_QUOTA_WEEKLY, KIND_QUOTA_MULTI})
# 不可自动恢复的额度类(策略 manual_ack)
QUOTA_MANUAL_KINDS = frozenset({KIND_QUOTA_CREDITS, KIND_QUOTA_SPEND_CONTROL, KIND_QUOTA_UNKNOWN})
# 直接进入 NEEDS_HUMAN 的类别(非额度类)
NEEDS_HUMAN_KINDS = frozenset({KIND_ABORTED, KIND_TERMINAL, KIND_UNKNOWN_ERROR})

# 恢复策略(persistence §3 recovery_plans.strategy)
STRATEGY_WINDOW_RESET = "window_reset"
STRATEGY_MANUAL_ACK = "manual_ack"


@dataclass(frozen=True)
class WindowEvidence:
    """失败时刻的一个窗口快照(failure.evidence 的组成;跨快照比较的基线)。"""

    bucket_key: str
    window_kind: str
    used_percent: float | None
    resets_at: int | None
    window_minutes: int | None


@dataclass(frozen=True)
class DetectedFailure:
    """检测器输出的一次失败判定(尚未落库)。"""

    turn_id: str
    kind: str
    detected_at: int
    blocking_window_keys: tuple[str, ...] = ()
    windows: tuple[WindowEvidence, ...] = ()  # 判定所依据的窗口基线(快照摘要)
    evidence: dict[str, object] = field(default_factory=dict)  # 事件摘要等

    @property
    def strategy(self) -> str:
        if self.kind in QUOTA_WINDOW_KINDS:
            return STRATEGY_WINDOW_RESET
        return STRATEGY_MANUAL_ACK


@dataclass(frozen=True)
class HumanMessage:
    """不含我方 marker 的用户消息观察(G3/人工接管检测)。

    只携带时间戳与首 N 字符摘要 —— 全文不落库不进日志(NFR-09;
    queue-safety §7 evidence 约定)。
    """

    timestamp: int | None
    head: str  # 首 32 字符(截断,仅供人工核对)
    turn_id: str | None = None


@dataclass(frozen=True)
class SessionObservation:
    """一次增量读取后,检测器对单个会话的全部观察(调度器消费)。"""

    session_uuid: str
    failures: tuple[DetectedFailure, ...] = ()
    task_completed: tuple[dict[str, object], ...] = ()  # {turn_id, timestamp, ...}
    human_messages: tuple[HumanMessage, ...] = ()  # 本次增量内的用户消息
    transient_events: tuple[dict[str, object], ...] = ()  # 瞬态类观察(仅审计,不建失败)
    latest_rate_limits: tuple[dict[str, object], int] | None = None  # (payload, ts)
    last_event_at: int | None = None
    has_open_turn: bool = False
    read_errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """读取本身是否可判定(坏文件该会话本轮跳过,recovery §4.4)。"""

        return not self.read_errors
