"""多会话调度器:tick 循环、查询节奏、异常隔离、评估决策(P4-T5)。

单线程顺序模型(architecture §4/D3/D-SES-6):
    启动清扫 → 按账户额度刷新(节奏见 quota-monitoring §6)→ 逐会话检测
    (异常隔离:单会话故障跳过本轮,绝不中断整个 tick)→ JSONL 样本入库
    → 等待计划评估(G1..G12 决策)→ 池回收 → tick 审计。

Phase 4 边界:评估产出决策(DeliveryDecision),**不执行投递** ——
投递执行器属 Phase 5(P5-T2);ready=True 只审计记录 `delivery_ready`。

节奏(quota-monitoring §6 / early-recovery §3,全部可配):
- 账户无等待计划:regular_probe_interval(默认 300s);
- 存在 WAITING_RECOVERY 会话: intensive_interval(默认 60s);
- 查询失败退避 60→120→300s 封顶;手动触发(check/quota --refresh)不受限;
- 临近期:now ≥ not_before - 60s 强制刷一次(到点立即复查)。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codex_autopilot import __version__
from codex_autopilot.config import Config
from codex_autopilot.quota.model import QuotaThresholds
from codex_autopilot.quota.service import QuotaService
from codex_autopilot.recovery.detector import FailureDetector
from codex_autopilot.recovery.early import EarlySignal, detect_signals
from codex_autopilot.recovery.evaluation import RecoveryEvaluator
from codex_autopilot.recovery.planner import RecoveryPlanner
from codex_autopilot.recovery.state_machine import (
    ACTIVE,
    FAILURE_DETECTED,
    R_UNKNOWN_QUOTA_KIND,
    WAITING_RECOVERY,
    RecoveryStateMachine,
)
from codex_autopilot.scheduling.appserver_pool import AppServerPool
from codex_autopilot.scheduling.sweep import SweepResult, connection_alive, startup_sweep
from codex_autopilot.store import Store
from codex_autopilot.store.dao import QuotaSnapshotDao, RuntimeFlagsDao
from codex_autopilot.store.recovery_dao import (
    PLAN_WAITING,
    FailureDao,
    RecoveryPlanDao,
)
from codex_autopilot.util.clock import TimeSource

logger = logging.getLogger(__name__)

GLOBAL_PAUSE_KEY = "global_pause"
NEAR_RESET_LEAD_SEC = 60  # early-recovery §3 临近期提前量
BACKOFF_STEPS = (60, 120, 300)  # 查询失败退避(封顶 300)

R_CREDITS_NO_RESET = "credits_or_spend_control_no_reset"


@dataclass
class _AccountPacing:
    last_refresh_epoch: int = 0
    consecutive_failures: int = 0


@dataclass
class SessionTickOutcome:
    uuid: str
    name: str
    state_before: str | None
    state_after: str | None = None
    actions: list[str] = field(default_factory=list)
    faults: list[str] = field(default_factory=list)


@dataclass
class TickResult:
    """一次 tick 的结构化结果(CLI --json 与测试断言消费)。"""

    started_at: int
    finished_at: int | None = None
    restricted: bool = False
    restricted_reason: str | None = None
    global_pause: bool = False
    accounts_refreshed: list[str] = field(default_factory=list)
    refresh_errors: list[str] = field(default_factory=list)
    sessions: list[SessionTickOutcome] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    early_signals: list[dict[str, Any]] = field(default_factory=list)
    faults: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "restricted": self.restricted,
            "restricted_reason": self.restricted_reason,
            "global_pause": self.global_pause,
            "accounts_refreshed": self.accounts_refreshed,
            "refresh_errors": self.refresh_errors,
            "sessions": [
                {
                    "uuid": s.uuid,
                    "name": s.name,
                    "state_before": s.state_before,
                    "state_after": s.state_after,
                    "actions": s.actions,
                    "faults": s.faults,
                }
                for s in self.sessions
            ],
            "decisions": self.decisions,
            "early_signals": self.early_signals,
            "faults": self.faults,
        }


class Scheduler:
    """tick 引擎;实例横跨 tick 存活(游标/节奏状态在内存)。"""

    def __init__(
        self,
        store: Store,
        config: Config,
        clock: TimeSource,
        *,
        use_pool: bool = True,
        client_factory=None,
    ):
        self._store = store
        self._config = config
        self._clock = clock
        self._pool = AppServerPool(clock=clock, client_version=__version__, idle_timeout_sec=600.0)
        self._pool.set_binary(config.codex_binary)
        self._quota = QuotaService(
            store, config, clock, client_factory=client_factory or self._pooled_factory
        )
        self._detector = FailureDetector(
            QuotaThresholds(
                exhausted_threshold_pct=config.quota.exhausted_threshold_pct,
                hint_min_pct=config.quota.hint_min_pct,
            )
        )
        self._machine = RecoveryStateMachine(store, clock)
        self._planner = RecoveryPlanner(clock)
        self._evaluator = RecoveryEvaluator(store, config, clock)
        self._failures = FailureDao(store)
        self._plans = RecoveryPlanDao(store)
        self._flags = RuntimeFlagsDao(store)
        self._snapshots = QuotaSnapshotDao(store)
        self._pacing: dict[int, _AccountPacing] = {}
        self._sweep: SweepResult | None = None
        # 本 tick 内各会话的检测观察(评估阶段 G3/G7 消费;每 tick 重置)
        self._observations: dict[str, Any] = {}
        # 测试注入专用(绕过池;保留一次性客户端)
        self._custom_factory = client_factory

    # -- 对外 -------------------------------------------------------------

    def tick(
        self,
        *,
        actor: str = "daemon",
        force_quota_refresh: bool = False,
    ) -> TickResult:
        started = self._clock.epoch()
        result = TickResult(started_at=started)

        if self._sweep is None:
            self._sweep = startup_sweep(self._store, self._clock, actor=actor)
            if not self._sweep.ok:
                result.restricted = True
                result.restricted_reason = self._sweep.restricted_reason
                result.finished_at = self._clock.epoch()
                return result
        if not connection_alive(self._store):
            result.restricted = True
            result.restricted_reason = "connection_lost"
            result.finished_at = self._clock.epoch()
            return result

        global_pause = bool(self._flags.get(GLOBAL_PAUSE_KEY))
        result.global_pause = global_pause
        accounts = self._quota.monitored_accounts()
        sessions = self._store.query(
            "SELECT * FROM sessions WHERE deleted_at IS NULL ORDER BY name"
        )
        sessions_by_account: dict[int, list[Any]] = {}
        for row in sessions:
            sessions_by_account.setdefault(int(row["account_id"]), []).append(row)

        # -- 阶段 A:按账户刷新额度(节奏控制;异常隔离) --------------------
        notifications_by_account: dict[int, tuple[str, ...]] = {}
        now = started
        for account in accounts:
            account_id = int(account["id"])
            waiting = self._failures.waiting_by_account(account_id)
            try:
                refreshed = self._refresh_account(
                    account,
                    waiting=waiting,
                    force=force_quota_refresh,
                    now=now,
                )
            except Exception as exc:
                result.refresh_errors.append(f"{account['codex_home']}: {exc}")
                self._fault(f"account refresh failed: {account['codex_home']}: {exc}")
                continue
            if refreshed:
                result.accounts_refreshed.append(account["codex_home"])
            # E4 通知按账户归属(避免跨账户误触发)
            notifications_by_account[account_id] = tuple(
                notification.get("method")
                for notification in self._quota.last_server_notifications
                if isinstance(notification.get("method"), str)
            )

        # -- 阶段 B:逐会话检测(异常隔离) --------------------------------
        samples: list[tuple[int, dict[str, Any], int]] = []
        self._observations = {}
        for row in sessions:
            outcome = SessionTickOutcome(
                uuid=row["uuid"],
                name=row["name"],
                state_before=self._machine.current(row["uuid"])["state"]
                if self._machine.current(row["uuid"]) is not None
                else None,
            )
            account = account_by_home_lookup(accounts, row)
            try:
                self._process_session(row, account, outcome, samples)
            except Exception as exc:
                outcome.faults.append(str(exc))
                self._fault(
                    f"session processing failed: {row['name']}: {exc}",
                    session_uuid=row["uuid"],
                )
            outcome.state_after = self._machine.state_of(row["uuid"])
            result.sessions.append(outcome)

        # -- 阶段 C:JSONL 样本入库(增量解析产物) ------------------------
        for account_id, payload, captured_at in samples:
            try:
                self._quota.ingest_jsonl_sample(account_id, payload, captured_at=captured_at)
            except Exception as exc:
                result.faults.append(f"jsonl ingest failed: {exc}")
                self._fault(f"jsonl ingest failed: {exc}")

        # -- 阶段 D:等待计划评估(提前恢复信号 + 决策) --------------------
        for row in sessions:
            try:
                self._evaluate_waiting(
                    row,
                    result,
                    notifications_seen=notifications_by_account.get(int(row["account_id"]), ()),
                )
            except Exception as exc:
                result.faults.append(f"evaluation failed for {row['name']}: {exc}")
                self._fault(f"evaluation failed: {row['name']}: {exc}", session_uuid=row["uuid"])

        # -- 阶段 E:池空闲回收 + 汇总审计 ---------------------------------
        self._pool.recycle_idle()
        result.finished_at = self._clock.epoch()
        self._store.audit(
            actor=actor,
            category="tick",
            level="info",
            summary=(
                f"tick done: {len(result.sessions)} session(s),"
                f" {len(result.accounts_refreshed)} refresh(s),"
                f" {len(result.decisions)} decision(s)"
            ),
            data={"restricted": result.restricted, "faults": len(result.faults)},
        )
        return result

    def close(self) -> None:
        self._pool.stop_all()

    # -- 阶段实现 ---------------------------------------------------------

    def _pooled_factory(self, binary: str, codex_home: Path):
        row = self._store.query_one(
            "SELECT id FROM accounts WHERE codex_home = ?", (str(codex_home),)
        )
        if row is None:
            raise RuntimeError(f"no account for codex_home {codex_home}")
        return self._pool.lease(int(row["id"]), codex_home)

    def _refresh_account(self, account, *, waiting, force: bool, now: int) -> bool:
        """按节奏决定是否刷新;返回是否执行了查询。"""

        account_id = int(account["id"])
        pacing = self._pacing.setdefault(account_id, _AccountPacing())
        if force:
            # 手动触发不受节奏限制(quota-monitoring §6);失败同样计入退避
            snapshot = self._quota.refresh_appserver(account, actor="daemon")
            self._record_refresh(pacing, ok=snapshot.ok, now=now)
            return True
        interval = (
            self._config.scheduler.intensive_interval
            if waiting
            else self._config.quota.regular_probe_interval
        )
        if pacing.consecutive_failures:
            backoff = BACKOFF_STEPS[min(pacing.consecutive_failures - 1, len(BACKOFF_STEPS) - 1)]
            interval = max(interval, backoff)
        if waiting:
            earliest = min(int(w["plan_not_before"]) for w in waiting)
            if now >= earliest - NEAR_RESET_LEAD_SEC:
                # 临近期:到点立即刷一次(early-recovery §3)
                interval = min(interval, NEAR_RESET_LEAD_SEC)
        if now - pacing.last_refresh_epoch < interval:
            return False
        snapshot = self._quota.refresh_appserver(account, actor="daemon")
        self._record_refresh(pacing, ok=snapshot.ok, now=now)
        return True

    @staticmethod
    def _record_refresh(pacing: _AccountPacing, *, ok: bool, now: int) -> None:
        pacing.last_refresh_epoch = now
        pacing.consecutive_failures = 0 if ok else pacing.consecutive_failures + 1

    def _process_session(self, row, account, outcome: SessionTickOutcome, samples) -> None:
        session_uuid = row["uuid"]
        codex_home = account["codex_home"]
        grace = self._config.recovery.missing_grace_sec
        state_row = self._machine.current(session_uuid)
        state = state_row["state"] if state_row is not None else None

        # 1) 文件存在性(暂停会话也探测;session-management §4)
        chain = self._detector_chain(codex_home, session_uuid)
        if not chain:
            if state == "MISSING":
                if self._machine.missing_over_grace(session_uuid, grace_sec=grace):
                    outcome.actions.append("missing_over_grace")
            else:
                self._machine.mark_missing(session_uuid)
                outcome.actions.append("rollout_missing")
            return
        if state == "MISSING":
            restored = self._machine.missing_recovered(session_uuid)
            outcome.actions.append(f"rollout_restored:{restored}")
            state = restored

        # 2) 暂停:冻结一切自动迁移;只保留存在性探测(§4)
        if not row["enabled"]:
            outcome.actions.append("paused_observed")
            return

        # 3) 增量读取与观察
        observation = self._detector.observe(
            Path(codex_home), session_uuid, now=self._clock.epoch()
        )
        self._observations[session_uuid] = observation
        if not observation.ok:
            outcome.faults.extend(observation.read_errors)
            self._fault(
                f"rollout read errors: {observation.read_errors}", session_uuid=session_uuid
            )
            return
        if observation.latest_rate_limits is not None:
            payload, ts = observation.latest_rate_limits
            samples.append(
                (int(row["account_id"]), payload, ts if ts is not None else self._clock.epoch())
            )

        # 4) 任务完成观察(S-26:记录;通知框架属 Phase 6)
        for completed in observation.task_completed:
            self._store.audit(
                actor="daemon",
                category="state",
                level="info",
                summary=f"task completed (turn {completed.get('turn_id')})",
                session_uuid=session_uuid,
                data={"turn_id": completed.get("turn_id"), "ts": completed.get("timestamp")},
            )
            outcome.actions.append("task_completed")

        # 4.5) 瞬态类观察:仅审计(recovery §4.2:v1 记录,不自动续)
        for transient in observation.transient_events:
            self._store.audit(
                actor="daemon",
                category="state",
                level="warn",
                summary=(
                    f"transient turn error observed: {transient.get('error_variant')}"
                    " (recorded only, no auto-resume in v1)"
                ),
                session_uuid=session_uuid,
                data={
                    "turn_id": transient.get("turn_id"),
                    "category": transient.get("category"),
                    "ts": transient.get("timestamp"),
                },
            )
            outcome.actions.append(f"transient:{transient.get('category')}")

        # 5) 人工接管(G3 观察侧):open failure 之后出现用户消息 → SUPERSEDED
        failure = self._failures.open_for_session(session_uuid)
        if failure is not None:
            baseline_end = _failure_turn_end(failure) or int(failure["detected_at"])
            humans = [
                message
                for message in observation.human_messages
                if (message.timestamp or self._clock.epoch()) > baseline_end
            ]
            if humans:
                self._machine.supersede(
                    session_uuid,
                    {
                        "messages": [{"timestamp": m.timestamp, "head": m.head} for m in humans],
                        "failure_id": int(failure["id"]),
                    },
                )
                outcome.actions.append("superseded")
                failure = None

        # 6) 新失败(仅 ACTIVE;其他状态由既有失败占据)
        state = self._machine.state_of(session_uuid) or ACTIVE
        if state == ACTIVE:
            for failure_detected in observation.failures:
                action = self._record_and_plan(session_uuid, failure_detected, outcome)
                if action:
                    state = self._machine.state_of(session_uuid) or state
                    if state != ACTIVE:
                        break
            if state == ACTIVE and self._failures.open_for_session(session_uuid) is None:
                unclosed = self._detector.unclosed_turn_failure(
                    session_uuid, now=self._clock.epoch()
                )
                if unclosed is not None:
                    self._record_and_plan(session_uuid, unclosed, outcome)
        elif state == FAILURE_DETECTED and failure is not None:
            # 崩溃残留(建计划前中断):补建计划(recovery §8)
            plan = self._plans.for_failure(int(failure["id"]))
            if plan is None:
                self._plan_existing(session_uuid, failure, outcome)

        # 7) 崩溃残留:WAITING 但计划缺失败联动(计划被清扫为 expired 等)
        state = self._machine.state_of(session_uuid) or ACTIVE
        if state == WAITING_RECOVERY:
            failure = self._failures.open_for_session(session_uuid)
            if failure is None:
                self._machine.fail_to_human(
                    session_uuid,
                    "db_inconsistent",
                    {"reason": "waiting_without_open_failure"},
                )
                outcome.actions.append("needs_human:db_inconsistent")

    def _record_and_plan(self, session_uuid: str, failure_detected, outcome) -> bool:
        """落库失败 + 建计划;返回是否产生了状态推进。"""

        row, action = self._machine.record_failure_for(session_uuid, failure_detected)
        if action == "duplicate":
            return False
        failure_id = int(row["id"])
        if action == "needs_human":
            outcome.actions.append(f"needs_human:{failure_detected.kind}")
            return True
        outcome.actions.append(f"failure_detected:{failure_detected.kind}")
        # 计划
        planned = self._planner.plan(failure_detected)
        if planned.strategy == "window_reset":
            plan = self._plans.insert(
                failure_id,
                strategy=planned.strategy,
                not_before=planned.not_before,
                basis=planned.basis,
                now=self._clock.epoch(),
            )
            self._machine.arm_plan(session_uuid, failure_id, int(plan["id"]))
            outcome.actions.append(f"plan_armed:not_before={planned.not_before}")
            return True
        # manual_ack:无法制定自动计划 → NEEDS_HUMAN(fail-closed)
        reason = (
            R_UNKNOWN_QUOTA_KIND if failure_detected.kind == "quota_unknown" else R_CREDITS_NO_RESET
        )
        self._machine.fail_to_human(
            session_uuid, reason, {"failure_id": failure_id, "basis": planned.basis}
        )
        outcome.actions.append(f"needs_human:{reason}")
        return True

    def _plan_existing(self, session_uuid: str, failure_row, outcome) -> None:
        """FAILURE_DETECTED 残留的补计划:从落库证据重建 DetectedFailure 输入。"""

        evidence = json.loads(failure_row["evidence"] or "{}")
        windows = evidence.get("windows") or []
        blocking = json.loads(failure_row["blocking_window_keys"] or "[]")
        from codex_autopilot.recovery.model import DetectedFailure, WindowEvidence

        detected = DetectedFailure(
            turn_id=failure_row["turn_id"],
            kind=failure_row["kind"],
            detected_at=int(failure_row["detected_at"]),
            blocking_window_keys=tuple(blocking),
            windows=tuple(
                WindowEvidence(
                    bucket_key=w.get("bucket_key", ""),
                    window_kind=w.get("window_kind", "unknown"),
                    used_percent=w.get("used_percent"),
                    resets_at=w.get("resets_at"),
                    window_minutes=w.get("window_minutes"),
                )
                for w in windows
            ),
            evidence={
                "snapshot_captured_at": evidence.get("snapshot_captured_at"),
                "error_variant": evidence.get("error_variant"),
                "source": evidence.get("source", "jsonl"),
            },
        )
        planned = self._planner.plan(detected)
        if planned.strategy == "window_reset":
            plan = self._plans.insert(
                int(failure_row["id"]),
                strategy=planned.strategy,
                not_before=planned.not_before,
                basis=planned.basis,
                now=self._clock.epoch(),
            )
            self._machine.arm_plan(session_uuid, int(failure_row["id"]), int(plan["id"]))
            outcome.actions.append("plan_armed:residual")
        else:
            reason = (
                R_UNKNOWN_QUOTA_KIND
                if failure_row["kind"] == "quota_unknown"
                else R_CREDITS_NO_RESET
            )
            self._machine.fail_to_human(
                session_uuid, reason, {"failure_id": int(failure_row["id"]), "residual": True}
            )
            outcome.actions.append(f"needs_human:{reason}")

    def _evaluate_waiting(self, row, result: TickResult, *, notifications_seen) -> None:
        session_uuid = row["uuid"]
        state = self._machine.state_of(session_uuid)
        if state != WAITING_RECOVERY:
            return
        failure = self._failures.open_for_session(session_uuid)
        if failure is None:
            return
        plan = self._plans.for_failure(int(failure["id"]))
        if plan is None or plan["status"] != PLAN_WAITING:
            return
        now = self._clock.epoch()

        # 提前恢复信号:失败时刻基线 vs 当前最新快照(early-recovery §2)
        baseline = self._snapshots.latest_ok_before(
            int(row["account_id"]), int(failure["detected_at"])
        )
        signals: list[EarlySignal] = []
        if baseline is not None:
            current = self._snapshots.latest_ok(int(row["account_id"]), source="appserver")
            if current is not None and current["captured_at"] > baseline["captured_at"]:
                from codex_autopilot.recovery.evaluation import _domain_snapshot_from_row

                signals = detect_signals(
                    _domain_snapshot_from_row(baseline),
                    _domain_snapshot_from_row(current),
                    resume_max_pct=self._config.quota.resume_max_pct,
                    updated_methods=tuple(notifications_seen),
                )
                for signal in signals:
                    result.early_signals.append(
                        {"session": row["name"], "signal": signal.signal, "detail": signal.detail}
                    )

        due = now >= int(plan["not_before"])
        if not due and not signals:
            return
        observation = self._observations.get(session_uuid)
        decision = self._evaluator.evaluate(
            row,
            failure,
            plan,
            global_pause=bool(self._flags.get(GLOBAL_PAUSE_KEY)),
            lock_held=True,
            integrity_ok=bool(self._sweep and self._sweep.ok),
            human_messages=observation.human_messages if observation is not None else (),
            has_open_turn=observation.has_open_turn if observation is not None else None,
            early_signals=tuple(signals),
        )
        entry: dict[str, Any] = {
            "session": row["name"],
            "failure_id": decision.failure_id,
            "plan_id": decision.plan_id,
            "ready": decision.ready,
            "due": due,
            "signals": [s.signal for s in signals],
            "blocked_gates": [
                {"gate": g.gate, "status": g.status, "detail": g.detail}
                for g in decision.gates
                if g.status != "pass"
            ],
            "gates": [
                {"gate": g.gate, "status": g.status, "detail": g.detail} for g in decision.gates
            ],
        }
        result.decisions.append(entry)
        if decision.ready:
            # Phase 4:只记录决策,不投递(投递执行器属 Phase 5)
            self._store.audit(
                actor="daemon",
                category="delivery",
                level="info",
                summary=(
                    "delivery decision: ready (delivery executor arrives in Phase 5;"
                    " no message sent)"
                ),
                session_uuid=session_uuid,
                data={
                    "failure_id": decision.failure_id,
                    "signals": [s.signal for s in signals],
                },
            )

    # -- 辅助 -------------------------------------------------------------

    @staticmethod
    def _detector_chain(codex_home: str, session_uuid: str):
        from codex_autopilot.codexif.discovery import find_rollout_chain

        try:
            return find_rollout_chain(Path(codex_home), session_uuid)
        except Exception:
            logger.warning("rollout discovery failed for %s", session_uuid, exc_info=True)
            return []

    def _fault(self, message: str, *, session_uuid: str | None = None) -> None:
        logger.warning("%s", message)
        self._store.audit(
            actor="daemon",
            category="fault",
            level="error",
            summary=message,
            session_uuid=session_uuid,
        )


def account_by_home_lookup(accounts, row):
    for account in accounts:
        if int(account["id"]) == int(row["account_id"]):
            return account
    raise RuntimeError(f"account {row['account_id']} not found for session {row['name']}")


def _failure_turn_end(failure) -> int | None:
    try:
        evidence = json.loads(failure["evidence"] or "{}")
        for key in ("task_completed_at", "turn_started_at"):
            value = evidence.get(key)
            if isinstance(value, int):
                return value
    except ValueError:
        return None
    return None
