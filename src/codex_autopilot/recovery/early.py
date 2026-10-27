"""提前恢复信号检测(E1-E4,early-recovery.md §2;P4-T6 观察侧)。

信号全部只读,按确定性排序:
- E1 `ordinaryUsageAllowed` false→true(后端权威放行,最权威);
- E2 窗口滚动:同 bucket_key 窗口 resets_at 变化,或 used_percent 骤降
  (较上次快照下降 > 恢复下限差值,即跌破 resume_max_pct);
- E3 重置券已兑换:`rateLimitResetCredits` 摘要观察(可用数量减少或
  available → redeemed/redeeming)—— 用户手动兑换后的自然结果,
  **绝不调用 consume**(FR-07 红线,D-ER-2);
- E4 服务端推送额度更新通知(method 常量见 codexif.appserver;
  免轮询信号;不可用退回定时轮询)。

任一信号只负责"把评估提前",不放宽任何守门(D-ER-1:与到期恢复同路径)。
"""

from __future__ import annotations

from dataclasses import dataclass

from codex_autopilot.codexif.appserver import NOTIFICATION_RATE_LIMITS_UPDATED
from codex_autopilot.quota.model import QuotaSnapshot

SIGNAL_E1 = "E1"
SIGNAL_E2 = "E2"
SIGNAL_E3 = "E3"
SIGNAL_E4 = "E4"

RATE_LIMITS_UPDATED_METHOD = NOTIFICATION_RATE_LIMITS_UPDATED

_REDEEMED_STATES = ("redeemed", "redeeming")


@dataclass(frozen=True)
class EarlySignal:
    signal: str  # 'E1'..'E4'
    detail: dict[str, object]


def detect_signals(
    baseline: QuotaSnapshot,
    current: QuotaSnapshot,
    *,
    resume_max_pct: int,
    updated_methods: tuple[str, ...] = (),
) -> list[EarlySignal]:
    """对比 基线快照(失败时刻附近)与 当前快照(等待期新采集)。

    `updated_methods`:自上次评估以来观察到的服务端通知 method 列表(E4;
    由 QuotaService.last_server_notifications 汇聚,调度器传入)。
    """

    signals: list[EarlySignal] = []

    # E1:权威放行标志翻转
    if baseline.ordinary_usage_allowed is False and current.ordinary_usage_allowed is True:
        signals.append(
            EarlySignal(
                SIGNAL_E1,
                {"ordinary_usage_allowed": [False, True], "captured_at": current.captured_at},
            )
        )

    # E2:窗口滚动 / 用量骤降(同 bucket_key)
    current_by_key: dict[str, list] = {}
    for window in current.windows:
        current_by_key.setdefault(window.bucket_key, []).append(window)
    for base_window in baseline.windows:
        for window in current_by_key.get(base_window.bucket_key, []):
            rolled = (
                base_window.resets_at is not None
                and window.resets_at is not None
                and window.resets_at != base_window.resets_at
            )
            dropped = (
                base_window.used_percent is not None
                and window.used_percent is not None
                and base_window.used_percent >= resume_max_pct
                and window.used_percent < resume_max_pct
            )
            if rolled or dropped:
                signals.append(
                    EarlySignal(
                        SIGNAL_E2,
                        {
                            "bucket_key": base_window.bucket_key,
                            "resets_at": [base_window.resets_at, window.resets_at],
                            "used_percent": [base_window.used_percent, window.used_percent],
                        },
                    )
                )
                break  # 同窗口一个证据即够

    # E3:重置券状态观察(只读)
    base_credits, curr_credits = baseline.reset_credits, current.reset_credits
    if base_credits is not None and curr_credits is not None:
        count_drop = (
            base_credits.available_count is not None
            and curr_credits.available_count is not None
            and curr_credits.available_count < base_credits.available_count
        )
        base_available = sum(1 for status in base_credits.statuses if status == "available")
        curr_available = sum(1 for status in curr_credits.statuses if status == "available")
        redeemed_increase = sum(
            1 for status in curr_credits.statuses if status in _REDEEMED_STATES
        ) > sum(1 for status in base_credits.statuses if status in _REDEEMED_STATES)
        if count_drop or curr_available < base_available or redeemed_increase:
            signals.append(
                EarlySignal(
                    SIGNAL_E3,
                    {
                        "available_count": [
                            base_credits.available_count,
                            curr_credits.available_count,
                        ],
                        "available_statuses": [base_available, curr_available],
                    },
                )
            )

    # E4:服务端推送通知
    if RATE_LIMITS_UPDATED_METHOD in updated_methods:
        signals.append(EarlySignal(SIGNAL_E4, {"method": RATE_LIMITS_UPDATED_METHOD}))

    return signals
