"""恢复评估器:G6 三证据恢复判定 + 投递前守门评估(P4;queue-safety §2)。

Phase 4 边界:评估产出 **决策**(DeliveryDecision),不执行投递 ——
投递执行器(G1..G12 全量通过后的 attempting 落库 + spawn)属 Phase 5。
守门逐项返回结果,任何一项"无法评估"(而非明确失败)同样不通过
(queue-safety §2)。

G6 恢复三证据(quota-monitoring §5.2,全部满足):
1. 窗口滚动证据:更新的快照中同 bucket_key 窗口 resets_at 变化或
   used_percent < resume_max_pct;
2. 证据新鲜:该快照 source=appserver 且非 stale(历史 JSONL 永远不是
   恢复证据,D-QM-6);
3. 账户级放行:ordinaryUsageAllowed 为 true(为 null 时退回 1+2 并记录)。

取"最新一条 appserver ok 快照"作为证据快照,要求它同时解除全部阻塞
窗口(比逐窗各取证据更严,fail-closed)。
"""

from __future__ import annotations

import json
import string
from dataclasses import dataclass
from typing import Any

from codex_autopilot.codexif.discovery import find_rollout_chain
from codex_autopilot.config import Config
from codex_autopilot.quota.model import QuotaSnapshot
from codex_autopilot.recovery.early import EarlySignal
from codex_autopilot.recovery.model import QUOTA_WINDOW_KINDS, HumanMessage
from codex_autopilot.store import Store
from codex_autopilot.store.dao import QuotaSnapshotDao
from codex_autopilot.store.recovery_dao import DeliveryDao
from codex_autopilot.util.clock import TimeSource
from codex_autopilot.util.identifiers import is_canonical_uuid

# 守门结果状态
G_PASS = "pass"
G_FAIL = "fail"
G_CANNOT_EVALUATE = "cannot_evaluate"

# G10 模板约束(queue-safety §2 G10;§3 的 8192 为含 marker 的消息上限,P5)
MAX_TEMPLATE_CHARS = 2000
TEMPLATE_PLACEHOLDERS = frozenset(
    {"session_name", "failure_kind", "failure_id", "detected_at_local"}
)


@dataclass(frozen=True)
class GateResult:
    gate: str  # 'G1'..'G12'
    status: str  # pass | fail | cannot_evaluate
    detail: str


@dataclass(frozen=True)
class DeliveryDecision:
    """一次对等待计划的评估结论(只读;调度器消费)。"""

    session_uuid: str
    failure_id: int
    plan_id: int | None
    ready: bool
    gates: tuple[GateResult, ...]
    early_signals: tuple[EarlySignal, ...] = ()
    g6_reason: str | None = None
    notes: tuple[str, ...] = ()

    def gate(self, name: str) -> GateResult:
        for result in self.gates:
            if result.gate == name:
                return result
        raise KeyError(name)


class RecoveryEvaluator:
    def __init__(self, store: Store, config: Config, clock: TimeSource):
        self._store = store
        self._config = config
        self._clock = clock
        self._snapshots = QuotaSnapshotDao(store)
        self._deliveries = DeliveryDao(store)

    # -- G6:恢复三证据 ----------------------------------------------------

    def recovery_evidence(
        self, failure_row: Any, account_id: int
    ) -> tuple[bool, str, QuotaSnapshot | None]:
        """返回 (通过, 原因码, 证据快照)。manual_ack 类恒不通过。"""

        kind = failure_row["kind"]
        if kind not in QUOTA_WINDOW_KINDS:
            return False, "manual_ack_kind", None
        blocking = json.loads(failure_row["blocking_window_keys"] or "[]")
        evidence = json.loads(failure_row["evidence"] or "{}")
        baseline_windows = evidence.get("windows") or []
        baseline_ts = evidence.get("snapshot_captured_at")
        baseline_ts = (
            baseline_ts if isinstance(baseline_ts, int) else int(failure_row["detected_at"])
        )
        if not blocking or not baseline_windows:
            return False, "no_blocking_windows", None

        now = self._clock.epoch()
        candidates = self._snapshots.ok_snapshots_since(account_id, baseline_ts, source="appserver")
        if not candidates:
            return False, "no_newer_appserver_snapshot", None
        latest = candidates[-1]
        if now - latest["captured_at"] > self._config.quota.fresh_ttl_sec:
            return False, "evidence_stale", None
        if latest.get("ordinary_usage_allowed") is False:
            return False, "ordinary_usage_not_allowed", None

        current_windows: dict[str, list[dict[str, Any]]] = {}
        for window in latest["windows"]:
            current_windows.setdefault(window["bucket_key"], []).append(window)
        resume_max = self._config.quota.resume_max_pct
        baseline_by_key = {w["bucket_key"]: w for w in baseline_windows}

        for key in blocking:
            base = baseline_by_key.get(key)
            candidates_for_key = current_windows.get(key)
            if base is None:
                return False, "baseline_window_missing", None
            if not candidates_for_key:
                return False, "window_absent_in_evidence", None
            if not any(_window_cleared(w, base, resume_max) for w in candidates_for_key):
                return False, "window_not_cleared", None

        snapshot = _domain_snapshot_from_row(latest)
        return True, "three_evidence_ok", snapshot

    # -- 全守门评估 --------------------------------------------------------

    def evaluate(
        self,
        session_row: Any,
        failure_row: Any,
        plan_row: Any | None,
        *,
        global_pause: bool,
        lock_held: bool,
        integrity_ok: bool,
        human_messages: tuple[HumanMessage, ...] = (),
        has_open_turn: bool | None = None,
        early_signals: tuple[EarlySignal, ...] = (),
    ) -> DeliveryDecision:
        gates: list[GateResult] = []
        failure_id = int(failure_row["id"])
        session_uuid = session_row["uuid"]

        # G1:failure 仍 open/planned 且未被取代
        if failure_row["status"] in ("open", "planned"):
            gates.append(GateResult("G1", G_PASS, f"status={failure_row['status']}"))
        else:
            gates.append(GateResult("G1", G_FAIL, f"status={failure_row['status']}"))

        # G2:rollout 文件存在(按会话所属账户的 codex_home)
        try:
            account = self._store.query_one(
                "SELECT codex_home FROM accounts WHERE id = ?", (session_row["account_id"],)
            )
            chain = (
                find_rollout_chain(account["codex_home"], session_uuid)
                if account is not None
                else []
            )
        except Exception:
            chain = None
        if chain:
            gates.append(GateResult("G2", G_PASS, f"{len(chain)} file(s)"))
        else:
            gates.append(GateResult("G2", G_FAIL, "no rollout file for uuid"))

        # G3:失败轮次之后无人工消息(P4 无 marker:任何 user 消息都算人工)
        if human_messages:
            gates.append(
                GateResult("G3", G_FAIL, f"{len(human_messages)} user message(s) after failure")
            )
        else:
            gates.append(GateResult("G3", G_PASS, "no human message"))

        # G4:无既有投递
        existing = self._deliveries.for_failure(failure_id)
        if existing:
            gates.append(GateResult("G4", G_FAIL, f"{len(existing)} existing delivery rows"))
        else:
            gates.append(GateResult("G4", G_PASS, "no delivery"))

        # G5:UUID canonical(来自注册记录)
        if is_canonical_uuid(session_uuid):
            gates.append(GateResult("G5", G_PASS, "canonical uuid"))
        else:
            gates.append(GateResult("G5", G_FAIL, "uuid not canonical"))

        # G6:恢复三证据
        passed, reason, _ = self.recovery_evidence(failure_row, int(session_row["account_id"]))
        if passed:
            gates.append(GateResult("G6", G_PASS, "three evidence ok"))
        else:
            gates.append(GateResult("G6", G_FAIL, reason))

        # G7:无进行中轮次(不确定视为在跑)
        if has_open_turn is None:
            gates.append(GateResult("G7", G_FAIL, "cannot evaluate turn state"))
        elif has_open_turn:
            gates.append(GateResult("G7", G_FAIL, "turn in progress"))
        else:
            gates.append(GateResult("G7", G_PASS, "no open turn"))

        # G8:enabled + auto_resume + 无全局暂停
        g8_reasons = []
        if not session_row["enabled"]:
            g8_reasons.append("session disabled")
        if not session_row["auto_resume"]:
            g8_reasons.append("auto_resume off")
        if global_pause:
            g8_reasons.append("global pause")
        gates.append(
            GateResult("G8", G_PASS if not g8_reasons else G_FAIL, "; ".join(g8_reasons) or "ok")
        )

        # G9:单实例锁
        gates.append(GateResult("G9", G_PASS if lock_held else G_CANNOT_EVALUATE, "scheduler lock"))

        # G10:模板渲染
        template = session_row["resume_template"] or self._config.recovery.template
        rendered, error = render_template(
            template,
            session_name=session_row["name"],
            failure_kind=failure_row["kind"],
            failure_id=failure_id,
            detected_at_local=_format_local(self._clock, int(failure_row["detected_at"])),
        )
        if error:
            gates.append(GateResult("G10", G_FAIL, error))
        else:
            gates.append(GateResult("G10", G_PASS, f"{len(rendered)} chars"))

        # G11:数据库完整性(本 tick 无 fault)
        gates.append(GateResult("G11", G_PASS if integrity_ok else G_FAIL, "integrity/fault check"))

        # G12:同会话最小投递间隔
        last = self._deliveries.last_for_session(session_uuid)
        if last is None:
            gates.append(GateResult("G12", G_PASS, "no prior delivery"))
        else:
            gap = self._clock.epoch() - int(last["started_at"])
            minimum = self._config.recovery.min_delivery_gap_sec
            if gap >= minimum:
                gates.append(GateResult("G12", G_PASS, f"gap {gap}s"))
            else:
                gates.append(GateResult("G12", G_FAIL, f"gap {gap}s < {minimum}s"))

        ready = all(g.status == G_PASS for g in gates)
        return DeliveryDecision(
            session_uuid=session_uuid,
            failure_id=failure_id,
            plan_id=int(plan_row["id"]) if plan_row is not None else None,
            ready=ready,
            gates=tuple(gates),
            early_signals=early_signals,
            g6_reason=None if passed else reason,
        )


def _window_cleared(window: dict[str, Any], baseline: dict[str, Any], resume_max: int) -> bool:
    """滚动证据:resets_at 变化 或 used_percent 跌破恢复下限。"""

    rolled = (
        baseline.get("resets_at") is not None
        and window.get("resets_at") is not None
        and window["resets_at"] != baseline["resets_at"]
    )
    dropped = (
        baseline.get("used_percent") is not None
        and window.get("used_percent") is not None
        and window["used_percent"] < resume_max
    )
    return rolled or dropped


def _domain_snapshot_from_row(row: dict[str, Any]) -> QuotaSnapshot:
    from codex_autopilot.quota.model import QuotaWindow, ResetCredits

    reset_credits = None
    if row.get("reset_credits"):
        try:
            data = json.loads(row["reset_credits"])
            reset_credits = ResetCredits(
                available_count=data.get("available_count"),
                statuses=tuple(data.get("statuses", ())),
            )
        except (TypeError, ValueError):
            reset_credits = None
    return QuotaSnapshot(
        source=row["source"],
        captured_at=row["captured_at"],
        ok=bool(row["ok"]),
        windows=tuple(
            QuotaWindow(
                bucket_key=w["bucket_key"],
                window_kind=w["window_kind"],
                kind_inferred=bool(w["kind_inferred"]),
                used_percent=w["used_percent"],
                resets_at=w["resets_at"],
                window_minutes=w["window_minutes"],
                is_exhausted=bool(w["is_exhausted"]),
            )
            for w in row["windows"]
        ),
        ordinary_usage_allowed=row.get("ordinary_usage_allowed"),
        spend_control_reached=bool(row.get("spend_control_reached", 0)),
        rate_limit_reached_type=row.get("rate_limit_reached_type"),
        reset_credits=reset_credits,
    )


def render_template(
    template: str,
    *,
    session_name: str,
    failure_kind: str,
    failure_id: int,
    detected_at_local: str,
) -> tuple[str, str | None]:
    """渲染续作消息模板;返回 (text, error)。白名单占位符,越界即配置错误。"""

    try:
        fields = [fname for _, fname, _, _ in string.Formatter().parse(template) if fname]
    except ValueError as exc:
        return "", f"template parse error: {exc}"
    unknown = [name for name in fields if name not in TEMPLATE_PLACEHOLDERS]
    if unknown:
        return "", f"unknown placeholder(s): {', '.join(sorted(unknown))}"
    try:
        text = template.format(
            session_name=session_name,
            failure_kind=failure_kind,
            failure_id=failure_id,
            detected_at_local=detected_at_local,
        )
    except (KeyError, IndexError, ValueError) as exc:
        return "", f"template render error: {exc}"
    if not text.strip():
        return "", "template renders empty"
    if len(text) > MAX_TEMPLATE_CHARS:
        return "", f"template exceeds {MAX_TEMPLATE_CHARS} chars"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return "", "template contains control characters"
    return text, None


def _format_local(clock: TimeSource, epoch: int) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
