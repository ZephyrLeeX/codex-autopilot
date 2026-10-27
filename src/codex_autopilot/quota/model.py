"""额度域模型:快照、窗口、耗尽/阻塞判定(P3-T2;quota-monitoring §1/§3/§5)。

三个严格区分的概念(任务书红线:99% ≠ 额度耗尽):
- `used_percent`:数据源给出的原始使用率;
- `is_exhausted`(窗口级)与 `AccountQuotaAssessment.blocked`(账户级):
  使用率经阈值判定的耗尽/阻塞状态,含 credits/spend_control/unknown 通路;
- `reference_resets_at`:调度参考时间 = max(阻塞窗口 resets_at),**不是**
  恢复保证(FR-03-A8:禁止当权威值;恢复判定另有三证据,quota-monitoring §5.2)。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

PARSER_VERSION = "quota/1"

# 窗口类型(persistence §3 quota_windows.window_kind)
KIND_FIVE_HOUR = "five_hour"
KIND_WEEKLY = "weekly"
KIND_CREDITS = "credits"
KIND_SPEND_CONTROL = "spend_control"
KIND_UNKNOWN = "unknown"

FIVE_HOUR_MINUTES = 300
WEEKLY_MINUTES = 10080

# 旧版本/其他来源可能携带的显式窗口类型字段值 → 标准类型;未列出 → unknown
_WINDOW_TYPE_VALUES = {
    "five_hour": KIND_FIVE_HOUR,
    "fivehour": KIND_FIVE_HOUR,
    "5h": KIND_FIVE_HOUR,
    "weekly": KIND_WEEKLY,
    "week": KIND_WEEKLY,
}

# rateLimitReachedType(实测 5 值)中属于 credits 类的子串
_CREDITS_MARKERS = ("credits_depleted",)


@dataclass(frozen=True)
class QuotaThresholds:
    """判定阈值(来自配置 quota 段;构造方负责区间校验)。"""

    exhausted_threshold_pct: int = 99  # ≥ 即 is_exhausted(S-02:99 与 100 同路径)
    hint_min_pct: int = 90  # 90-98 仅 hint(quota-monitoring §5.1)

    def __post_init__(self) -> None:
        if not (95 <= self.exhausted_threshold_pct <= 100):
            raise ValueError("exhausted_threshold_pct must be in [95, 100]")
        if not (90 <= self.hint_min_pct <= 98):
            raise ValueError("hint_min_pct must be in [90, 98]")
        if self.hint_min_pct >= self.exhausted_threshold_pct:
            raise ValueError("hint_min_pct must be lower than exhausted_threshold_pct")


@dataclass(frozen=True)
class QuotaWindow:
    """一个额度窗口(账户快照内一行;persistence §3 quota_windows)。"""

    bucket_key: str  # 跨源稳定标识:window_minutes 十进制串,或 "unknown:<n>"
    window_kind: str
    kind_inferred: bool  # True = 按位置推断(FR-03-A2,输出必须可见)
    used_percent: float | None
    resets_at: int | None
    window_minutes: int | None
    is_exhausted: bool = False  # used_percent ≥ 耗尽阈值(仅窗口级判定)
    hint: bool = False  # hint_min ≤ used_percent < 阈值(调度参考,非阻塞)
    limit_id: str | None = None  # appserver 额度桶名(codex/premium/…);jsonl 无
    position: str | None = None  # primary/secondary/…


@dataclass(frozen=True)
class QuotaSnapshot:
    """某账户某时刻的额度状态(来源:appserver 实时 / jsonl 历史)。"""

    source: str  # 'appserver' | 'jsonl'
    captured_at: int  # 数据自身时间(Unix 秒)
    ok: bool  # False = 解析失败(错误快照也留痕)
    error: str | None = None
    windows: tuple[QuotaWindow, ...] = ()
    ordinary_usage_allowed: bool | None = None  # appserver 顶级权威放行标志(可 None)
    plan_type: str | None = None  # 展示用,不参与判定
    rate_limit_reached_type: str | None = None
    spend_control_reached: bool = False
    account_email: str | None = None  # 展示用(脱敏与否由输出层决定,不主动打印)
    parser_version: str = PARSER_VERSION


@dataclass(frozen=True)
class AccountQuotaAssessment:
    """对快照的账户级判定(阻塞状态;与展示解耦,供 P4/P5 调度与守门消费)。"""

    blocked: bool
    blocked_kind: str  # 'none'|'windows'|'credits'|'spend_control'|'unknown'
    blocking_keys: tuple[str, ...]  # 阻塞窗口的 bucket_key(credits/spend_control 为空)
    reference_resets_at: int | None  # max(阻塞窗口 resets_at);unknown/credits 为 None
    unknown_reason: str | None  # blocked_kind='unknown' 时的稳定原因码
    ordinary_usage_allowed: bool | None = field(default=None)


def resolve_window_kind(
    *,
    window_minutes: int | None,
    window_type: str | None = None,
    position: str | None = None,
) -> tuple[str, bool]:
    """窗口类型判定(FR-03-A2,quota-monitoring §3)。返回 (kind, inferred)。"""

    if window_minutes == FIVE_HOUR_MINUTES:
        return KIND_FIVE_HOUR, False
    if window_minutes == WEEKLY_MINUTES:
        return KIND_WEEKLY, False
    if window_type:
        mapped = _WINDOW_TYPE_VALUES.get(str(window_type).strip().lower())
        if mapped is not None:
            return mapped, False
        return KIND_UNKNOWN, False
    # 结构化字段缺失 → 按 primary/secondary 位置推断,必须标记 inferred
    if position == "primary":
        return KIND_FIVE_HOUR, True
    if position == "secondary":
        return KIND_WEEKLY, True
    return KIND_UNKNOWN, False


def make_bucket_key(window_minutes: int | None, unknown_index: int) -> str:
    """bucket_key:分钟十进制串(跨源稳定);未知窗口编号 unknown:n。

    刻意不含 limit_id:JSONL 无桶名,含桶名会破坏"失败时(JSONL)→恢复证据
    (appserver)"的跨源同窗匹配(G6);同分钟多桶在快照内重复 key,展示层
    另带 limit_id 区分(见 quota-monitoring §8 实施记录)。
    """

    return str(window_minutes) if window_minutes is not None else f"unknown:{unknown_index}"


def apply_thresholds(window: QuotaWindow, thresholds: QuotaThresholds) -> QuotaWindow:
    """按阈值补全窗口的 is_exhausted/hint 标记(解析层与测试共用同一实现)。

    is_exhausted 仅是窗口级判定;账户级阻塞见 assess_snapshot。
    """

    used = window.used_percent
    is_exhausted = used is not None and used >= thresholds.exhausted_threshold_pct
    hint = used is not None and not is_exhausted and used >= thresholds.hint_min_pct
    if is_exhausted == window.is_exhausted and hint == window.hint:
        return window
    return replace(window, is_exhausted=is_exhausted, hint=hint)


def _classify_reached_type(reached_type: str | None) -> str | None:
    """rateLimitReachedType → 失败类别;None/纯 rate_limit_reached 返回 None(走窗口判定)。"""

    if not reached_type:
        return None
    value = str(reached_type)
    if any(marker in value for marker in _CREDITS_MARKERS):
        return KIND_CREDITS
    return None


def assess_snapshot(
    snapshot: QuotaSnapshot, *, thresholds: QuotaThresholds | None = None
) -> AccountQuotaAssessment:
    """账户级阻塞判定(quota-monitoring §5.1;fail-closed)。

    阻塞 = 任一窗口 is_exhausted,或任一窗口无法评估(unknown kind 或缺
    used_percent),或 credits 类,或 spendControlReached,或
    ordinaryUsageAllowed=false。
    ordinaryUsageAllowed=true **不**解除窗口阻塞(服务器预扣:98% 也可能失败,
    quota-monitoring §5.1);它是恢复三证据的第三道(P5 G6),不是放行开关。
    """

    thresholds = thresholds or QuotaThresholds()
    ordinary = snapshot.ordinary_usage_allowed

    if not snapshot.ok:
        return AccountQuotaAssessment(
            blocked=True,
            blocked_kind=KIND_UNKNOWN,
            blocking_keys=(),
            reference_resets_at=None,
            unknown_reason="snapshot_not_ok",
            ordinary_usage_allowed=ordinary,
        )

    blocking: list[QuotaWindow] = []
    for window in snapshot.windows:
        unverifiable = window.window_kind == KIND_UNKNOWN or window.used_percent is None
        if window.is_exhausted or unverifiable:
            blocking.append(window)

    credits_kind = _classify_reached_type(snapshot.rate_limit_reached_type)
    if credits_kind is not None:
        # credits/workspace 类:无 resets_at,只能人工或官方改变(§5.1)
        return AccountQuotaAssessment(
            blocked=True,
            blocked_kind=KIND_CREDITS,
            blocking_keys=(),
            reference_resets_at=None,
            unknown_reason=None,
            ordinary_usage_allowed=ordinary,
        )
    if snapshot.spend_control_reached:
        return AccountQuotaAssessment(
            blocked=True,
            blocked_kind=KIND_SPEND_CONTROL,
            blocking_keys=(),
            reference_resets_at=None,
            unknown_reason=None,
            ordinary_usage_allowed=ordinary,
        )

    if blocking:
        # 任一阻塞窗口不可评估(unknown kind / 缺 used_percent)⇒ 整体按 unknown:
        # G6 对 unknown 恒不通过(§5.2),失败只能 manual_ack;已知窗口的
        # resets_at 仍作为参考时间返回(供展示,不作投递授权)。
        has_unverifiable = any(
            w.window_kind == KIND_UNKNOWN or w.used_percent is None for w in blocking
        )
        resets = [w.resets_at for w in blocking if w.resets_at is not None]
        return AccountQuotaAssessment(
            blocked=True,
            blocked_kind=KIND_UNKNOWN if has_unverifiable else "windows",
            blocking_keys=tuple(dict.fromkeys(w.bucket_key for w in blocking)),
            reference_resets_at=max(resets) if resets else None,
            unknown_reason="unverifiable_window" if has_unverifiable else None,
            ordinary_usage_allowed=ordinary,
        )

    if not snapshot.windows and ordinary is None:
        # 空快照且无权威放行标志:无任何可证实数据 → unknown(S-20/T-QUOTA-09)
        return AccountQuotaAssessment(
            blocked=True,
            blocked_kind=KIND_UNKNOWN,
            blocking_keys=(),
            reference_resets_at=None,
            unknown_reason="empty_snapshot",
            ordinary_usage_allowed=None,
        )

    if ordinary is False:
        # 窗口全部正常但后端明确说"普通用量不被允许"→ 无法解释,按 unknown 保守阻塞
        return AccountQuotaAssessment(
            blocked=True,
            blocked_kind=KIND_UNKNOWN,
            blocking_keys=(),
            reference_resets_at=None,
            unknown_reason="ordinary_usage_not_allowed",
            ordinary_usage_allowed=False,
        )

    return AccountQuotaAssessment(
        blocked=False,
        blocked_kind="none",
        blocking_keys=(),
        reference_resets_at=None,
        unknown_reason=None,
        ordinary_usage_allowed=ordinary,
    )
