"""额度快照解析:双源双命名兼容(P3-T2;D-QM-1、T-COMPAT)。

两套字段命名(phase0-research 双实证,映射表集中此处、禁止散落):
- App Server(实时):camelCase `usedPercent/windowDurationMins/resetsAt`,
  顶层 `rateLimitsByLimitId`(新版,多桶)或 `rateLimits`(旧版回退,单快照);
- rollout JSONL(历史):snake_case `used_percent/window_minutes/resets_at`,
  载挂在 token_count 事件的 `rate_limits` 字段,无桶名。

解析策略(fail-closed):
- 形状不可解析 → ok=0 错误快照(留痕,上层降级),不抛异常到调用方;
- 全 null 的额度桶(实测 premium 常如此)整桶丢弃,不算 unknown;
- 白名单外字段(plan_type、credits、account 等展示字段以外)解析时丢弃。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from codex_autopilot.quota.model import (
    KIND_CREDITS,
    KIND_SPEND_CONTROL,
    PARSER_VERSION,
    QuotaSnapshot,
    QuotaThresholds,
    QuotaWindow,
    ResetCredits,
    apply_thresholds,
    make_bucket_key,
    resolve_window_kind,
)

# D-QM-1:camelCase → snake_case 的窗口字段别名,全项目唯一一处。
_WINDOW_FIELD_ALIASES = {
    "usedPercent": "used_percent",
    "windowDurationMins": "window_minutes",
    "resetsAt": "resets_at",
    "windowType": "window_type",
}

# rate_limits 层级的元字段(非窗口 dict),双命名。
_META_FIELD_ALIASES = {
    "rateLimitReachedType": "rate_limit_reached_type",
    "planType": "plan_type",
    "spendControlReached": "spend_control_reached",
}

# appserver 桶内已知窗口位置(schema:primary/secondary;多余位置按序号容忍)
_KNOWN_POSITIONS = ("primary", "secondary")

_CREDITS_TYPE_MARKERS = ("credits_depleted",)


def _canonical_keys(data: Mapping[str, Any], aliases: Mapping[str, str]) -> dict[str, Any]:
    """返回键名规范化后的浅拷贝:snake 原样,camel 按别名映射。"""

    out: dict[str, Any] = {}
    for key, value in data.items():
        out[aliases.get(key, key)] = value
    return out


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _parse_window(
    data: Mapping[str, Any], *, position: str, thresholds: QuotaThresholds, unknown_index: int
) -> QuotaWindow:
    canon = _canonical_keys(data, _WINDOW_FIELD_ALIASES)
    minutes = _as_int(canon.get("window_minutes"))
    resets_at = _as_int(canon.get("resets_at"))
    used = _as_float(canon.get("used_percent"))
    kind, inferred = resolve_window_kind(
        window_minutes=minutes,
        window_type=canon.get("window_type") if isinstance(canon.get("window_type"), str) else None,
        position=position,
    )
    window = QuotaWindow(
        bucket_key=make_bucket_key(minutes, unknown_index),
        window_kind=kind,
        kind_inferred=inferred,
        used_percent=used,
        resets_at=resets_at,
        window_minutes=minutes,
        position=position,
    )
    return apply_thresholds(window, thresholds)


def _window_has_data(window: QuotaWindow) -> bool:
    return any(
        value is not None
        for value in (window.used_percent, window.resets_at, window.window_minutes)
    )


def _parse_rate_limits_dict(
    data: Mapping[str, Any],
    *,
    thresholds: QuotaThresholds,
    limit_id: str | None,
) -> tuple[list[QuotaWindow], dict[str, Any]]:
    """一个额度桶(或 JSONL 的 rate_limits):{"primary": {...}, "secondary": {...}, …元字段}。"""

    canon = _canonical_keys(data, _META_FIELD_ALIASES)
    windows: list[QuotaWindow] = []
    unknown_seq = 0
    positions = list(_KNOWN_POSITIONS)
    extra = [k for k in canon if k not in positions and isinstance(canon.get(k), dict)]
    for key in positions + extra:
        raw = canon.get(key)
        if not isinstance(raw, dict):
            continue
        window = _parse_window(raw, position=key, thresholds=thresholds, unknown_index=unknown_seq)
        if window.window_minutes is None:
            unknown_seq += 1
        if _window_has_data(window):
            windows.append(dataclasses.replace(window, limit_id=limit_id))
    return windows, canon


def _parse_reset_credits(raw: Any) -> ResetCredits | None:
    """`rateLimitResetCredits` 摘要(early-recovery E3;形状坏 → None,不影响主解析)。"""

    if not isinstance(raw, Mapping):
        return None
    count = raw.get("availableCount")
    statuses = [
        str(credit["status"])
        for credit in raw.get("credits", [])
        if isinstance(credit, Mapping) and isinstance(credit.get("status"), str)
    ]
    if count is None and not statuses:
        return None
    return ResetCredits(
        available_count=count if isinstance(count, int) and not isinstance(count, bool) else None,
        statuses=tuple(statuses),
    )


def parse_appserver_response(
    response: Mapping[str, Any] | None,
    *,
    thresholds: QuotaThresholds,
    captured_at: int,
) -> QuotaSnapshot:
    """App Server 实时查询的 result → 快照。

    顶层:rateLimitsByLimitId(新)→ rateLimits(旧回退)→ 空;
    ordinaryUsageAllowed/account 白名单外展示字段保留,其余丢弃。
    形状坏 → ok=0。
    """

    if not isinstance(response, Mapping):
        return QuotaSnapshot(
            source="appserver",
            captured_at=captured_at,
            ok=False,
            error="response is not a JSON object",
            parser_version=PARSER_VERSION,
        )
    try:
        windows: list[QuotaWindow] = []
        spend_control = False
        reached_type: str | None = None
        plan_type: str | None = None

        buckets_raw = response.get("rateLimitsByLimitId")
        if buckets_raw is not None and not isinstance(buckets_raw, Mapping):
            return QuotaSnapshot(
                source="appserver",
                captured_at=captured_at,
                ok=False,
                error="rateLimitsByLimitId is not an object",
                parser_version=PARSER_VERSION,
            )
        buckets: list[tuple[str | None, Mapping[str, Any]]] = []
        if isinstance(buckets_raw, Mapping) and buckets_raw:
            # 新版:按 limit_id 分桶;跳过值不是 dict 的坏桶
            buckets = [
                (str(key), value)
                for key, value in buckets_raw.items()
                if isinstance(value, Mapping)
            ]
        else:
            legacy = response.get("rateLimits")
            if legacy is not None and not isinstance(legacy, Mapping):
                return QuotaSnapshot(
                    source="appserver",
                    captured_at=captured_at,
                    ok=False,
                    error="rateLimits is not an object",
                    parser_version=PARSER_VERSION,
                )
            if isinstance(legacy, Mapping) and legacy:
                buckets = [(None, legacy)]  # 旧版:单桶无 limit_id

        for limit_id, bucket in buckets:
            bucket_windows, canon = _parse_rate_limits_dict(
                bucket, thresholds=thresholds, limit_id=limit_id
            )
            if not bucket_windows:
                continue  # 全 null 桶(实测 premium 常如此)整桶丢弃
            windows.extend(bucket_windows)
            if canon.get("spend_control_reached") is True:
                spend_control = True
            bucket_reached = canon.get("rate_limit_reached_type")
            credits_like = isinstance(bucket_reached, str) and any(
                marker in bucket_reached for marker in _CREDITS_TYPE_MARKERS
            )
            # credits 类优先记录;否则记录首个非空值(展示)
            if (
                isinstance(bucket_reached, str)
                and bucket_reached
                and (reached_type is None or credits_like)
            ):
                reached_type = bucket_reached
            bucket_plan = canon.get("plan_type")
            if plan_type is None and isinstance(bucket_plan, str):
                plan_type = bucket_plan

        ordinary = response.get("ordinaryUsageAllowed")
        if not isinstance(ordinary, bool):
            ordinary = None
        account = response.get("account")
        email = None
        if isinstance(account, Mapping) and isinstance(account.get("email"), str):
            email = account["email"]

        return QuotaSnapshot(
            source="appserver",
            captured_at=captured_at,
            ok=True,
            windows=tuple(windows),
            ordinary_usage_allowed=ordinary,
            plan_type=plan_type,
            rate_limit_reached_type=reached_type,
            spend_control_reached=spend_control,
            reset_credits=_parse_reset_credits(response.get("rateLimitResetCredits")),
            account_email=email,
            parser_version=PARSER_VERSION,
        )
    except (TypeError, ValueError, AttributeError) as exc:
        return QuotaSnapshot(
            source="appserver",
            captured_at=captured_at,
            ok=False,
            error=f"unparseable rateLimits payload: {type(exc).__name__}",
            parser_version=PARSER_VERSION,
        )


def parse_jsonl_rate_limits(
    rate_limits: Mapping[str, Any] | None,
    *,
    thresholds: QuotaThresholds,
    captured_at: int,
) -> QuotaSnapshot:
    """token_count 事件的 `rate_limits` 载荷 → 快照(历史,source=jsonl)。

    JSONL 无 ordinaryUsageAllowed(那是 appserver 顶级字段)、无桶名;
    rate_limit_reached_type/plan_type 若存在则读取(实测样本含 snake 命名)。
    """

    if not isinstance(rate_limits, Mapping):
        return QuotaSnapshot(
            source="jsonl",
            captured_at=captured_at,
            ok=False,
            error="rate_limits payload is not a JSON object",
            parser_version=PARSER_VERSION,
        )
    try:
        windows, canon = _parse_rate_limits_dict(rate_limits, thresholds=thresholds, limit_id=None)
        reached = canon.get("rate_limit_reached_type")
        plan = canon.get("plan_type")
        spend = canon.get("spend_control_reached") is True
        return QuotaSnapshot(
            source="jsonl",
            captured_at=captured_at,
            ok=True,
            windows=tuple(windows),
            ordinary_usage_allowed=None,
            plan_type=plan if isinstance(plan, str) else None,
            rate_limit_reached_type=reached if isinstance(reached, str) else None,
            spend_control_reached=spend,
            parser_version=PARSER_VERSION,
        )
    except (TypeError, ValueError, AttributeError) as exc:
        return QuotaSnapshot(
            source="jsonl",
            captured_at=captured_at,
            ok=False,
            error=f"unparseable rate_limits payload: {type(exc).__name__}",
            parser_version=PARSER_VERSION,
        )


# 供测试/展示引用的类别常量(KIND_CREDITS/KIND_SPEND_CONTROL 复用 model 定义)
__all__ = [
    "KIND_CREDITS",
    "KIND_SPEND_CONTROL",
    "parse_appserver_response",
    "parse_jsonl_rate_limits",
]
