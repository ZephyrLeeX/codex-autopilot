"""恢复计划器:等待计划计算(P4-T4)。

规则(early-recovery §1、quota-monitoring §5.1、任务书 FR-03-A8):
- `not_before` 只是**调度参考**(何时可以开始重新评估/密集刷新),不是
  恢复保证;恢复判定永远走 G6 三证据,与到点无关;
- 双窗口阻塞取 max(resets_at)(S-05:等最晚);
- 恢复时间不确定的处理(fail-closed):
  - 任一阻塞窗口缺 resets_at / resets_at 非正 → 无法计算可靠参考 →
    策略 manual_ack(不自动投递);
  - resets_at 已过期(≤ now)→ 参考时间立即到期(仍需现场证据);
  - resets_at 异常偏晚(超出 快照时间 + 窗口长度 + 容差)→ 时间戳不可信,
    按窗口理论上限截断(cap)并在 basis 记录 capped 标记;
  - credits / spend_control / unknown 类无 resets_at 语义 → manual_ack。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codex_autopilot.recovery.model import (
    QUOTA_WINDOW_KINDS,
    STRATEGY_MANUAL_ACK,
    STRATEGY_WINDOW_RESET,
    DetectedFailure,
)
from codex_autopilot.util.clock import TimeSource

# resets_at 容差:快照后 窗口长度+容差 之内视为正常;超出视为异常时间戳
RESET_AT_SKEW_SEC = 3600


@dataclass(frozen=True)
class PlannedRecovery:
    """一次计划计算的结果(写入 recovery_plans 的载荷)。"""

    strategy: str
    not_before: int
    basis: dict = field(default_factory=dict)


class RecoveryPlanner:
    def __init__(self, clock: TimeSource):
        self._clock = clock

    def plan(self, failure: DetectedFailure) -> PlannedRecovery:
        now = self._clock.epoch()
        if failure.kind not in QUOTA_WINDOW_KINDS or not failure.windows:
            return PlannedRecovery(
                strategy=STRATEGY_MANUAL_ACK,
                not_before=now,
                basis={
                    "reason": "no_window_reset_basis",
                    "kind": failure.kind,
                    "computed_at": now,
                },
            )
        return self._plan_windows(failure, now)

    # -- 内部 -------------------------------------------------------------

    def _plan_windows(self, failure: DetectedFailure, now: int) -> PlannedRecovery:
        sample_ts = failure.evidence.get("snapshot_captured_at")
        sample_ts = sample_ts if isinstance(sample_ts, int) else None
        notes: list[str] = []
        effective: list[int] = []

        for window in failure.windows:
            raw = window.resets_at
            if raw is None or raw <= 0:
                # 任一阻塞窗口时间不确定 ⇒ 整体参考不可靠(fail-closed)
                return PlannedRecovery(
                    strategy=STRATEGY_MANUAL_ACK,
                    not_before=now,
                    basis={
                        "reason": "missing_or_invalid_resets_at",
                        "kind": failure.kind,
                        "window": window.bucket_key,
                        "computed_at": now,
                    },
                )
            value = raw
            if window.window_minutes is not None and sample_ts is not None:
                ceiling = sample_ts + window.window_minutes * 60 + RESET_AT_SKEW_SEC
                if value > ceiling:
                    value = ceiling
                    notes.append(f"resets_at_capped:{window.bucket_key}")
            if value <= now:
                # 已过期:参考时间立即到期,恢复仍需现场实时证据
                value = now
                notes.append(f"resets_at_expired:{window.bucket_key}")
            effective.append(value)

        not_before = max(effective)
        return PlannedRecovery(
            strategy=STRATEGY_WINDOW_RESET,
            not_before=not_before,
            basis={
                "kind": failure.kind,
                "computed_at": now,
                "max_resets_at": not_before,
                "windows": [
                    {
                        "bucket_key": w.bucket_key,
                        "window_kind": w.window_kind,
                        "used_percent": w.used_percent,
                        "resets_at": w.resets_at,
                        "effective_resets_at": value,
                        "window_minutes": w.window_minutes,
                    }
                    for w, value in zip(failure.windows, effective, strict=True)
                ],
                "notes": notes,
            },
        )
