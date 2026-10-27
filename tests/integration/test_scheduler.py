"""P4-T5/T6 调度器集成测试:多会话调度、状态隔离、提前恢复、故障注入。

组装:Fake app-server(剧本驱动)+ 隔离 rollout 文件 + FakeClock + 隔离 DB。
覆盖场景矩阵:S-01/02/05/06/07/08/10/14/15/20/24/28(调度侧)与
check --no-deliver 零投递副作用;L1 红线:全程无真实 codex 进程。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autopilot.config import load_config
from codex_autopilot.scheduling.scheduler import Scheduler
from codex_autopilot.store.dao import AccountDao, NewSession, SessionDao
from tests import helpers as h
from tests.conftest import write_scenario

UUID_A = "01234567-89ab-cdef-0123-456789abcdef"
UUID_B = "fedcba98-7654-3210-fedc-ba9876543210"

FIVE_HOUR = {"usedPercent": 100, "resetsAt": h.T0 + 18000, "windowDurationMins": 300}
WEEKLY_FULL = {"usedPercent": 100, "resetsAt": h.T0 + 600000, "windowDurationMins": 10080}
WEEKLY_LOW = {"usedPercent": 40, "resetsAt": h.T0 + 600000, "windowDurationMins": 10080}
RECOVERED_PRIMARY = {"usedPercent": 5, "resetsAt": h.T0 + 36000, "windowDurationMins": 300}
ROLLED_WEEKLY = {"usedPercent": 10, "resetsAt": h.T0 + 620000, "windowDurationMins": 10080}


class Harness:
    """调度器测试装配:注册会话 + 写 rollout + 换剧本 + 拨时钟。"""

    def __init__(self, store, fake_clock, config, scenario_path, fake_log):
        self.store = store
        self.clock = fake_clock
        self.config = config
        self.scenario = scenario_path
        self.fake_log = fake_log
        self._scheduler: Scheduler | None = None

    def register(self, uuid: str, name: str, *, auto_resume=True, enabled=True) -> None:
        home = str(self.config.codex_home)
        with self.store.transaction():
            account = AccountDao(self.store).get_or_create(home, label=None, now=self.clock.epoch())
            SessionDao(self.store).insert(
                NewSession(
                    uuid=uuid,
                    name=name,
                    project_path="/tmp/project",
                    account_id=int(account["id"]),
                    auto_resume=auto_resume,
                ),
                now=self.clock.epoch(),
            )
        if not enabled:
            self.store.execute("UPDATE sessions SET enabled = 0 WHERE uuid = ?", (uuid,))

    def write(
        self,
        uuid: str,
        lines: list[str],
        *,
        ts: str = "2026-01-01T00-00-00",
        suffix_uuid: str | None = None,
    ) -> Path:
        chain = uuid if suffix_uuid is None else f"{uuid}_{suffix_uuid}"
        directory = self.config.codex_home / "sessions" / "2026" / "01" / "01"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"rollout-{ts}-{chain}.jsonl"
        if path.exists():
            with path.open("a", encoding="utf-8") as handle:
                handle.write(h.file_content(lines))
        else:
            path.write_text(h.file_content(lines), encoding="utf-8")
        return path

    def scenario_exhausted(self, *, weekly=WEEKLY_LOW, credits=None, ordinary=None):
        payload = {
            "rateLimitsByLimitId": {
                "codex": {"primary": FIVE_HOUR, "secondary": weekly},
            },
        }
        if ordinary is not None:
            payload["ordinaryUsageAllowed"] = ordinary
        else:
            payload["ordinaryUsageAllowed"] = True
        if credits is not None:
            payload["rateLimitResetCredits"] = credits
        write_scenario(self.scenario, rate_limits_result=payload)

    def scenario_recovered(self, *, primary=RECOVERED_PRIMARY, weekly=WEEKLY_LOW, credits=None):
        payload = {
            "rateLimitsByLimitId": {"codex": {"primary": primary, "secondary": weekly}},
            "ordinaryUsageAllowed": True,
        }
        if credits is not None:
            payload["rateLimitResetCredits"] = credits
        write_scenario(self.scenario, rate_limits_result=payload)

    def tick(self, *, fresh_scheduler=False, force_refresh=False):
        if fresh_scheduler and self._scheduler is not None:
            self._scheduler.close()
            self._scheduler = None
        if self._scheduler is None:
            self._scheduler = Scheduler(self.store, self.config, self.clock)
        return self._scheduler.tick(force_quota_refresh=force_refresh)

    def state_of(self, uuid: str) -> str | None:
        row = self.store.query_one(
            "SELECT state FROM session_state WHERE session_uuid = ?", (uuid,)
        )
        return row["state"] if row is not None else None

    def open_failure(self, uuid: str):
        return self.store.query_one(
            "SELECT * FROM failures WHERE session_uuid = ? AND status IN ('open','planned')"
            " ORDER BY id DESC LIMIT 1",
            (uuid,),
        )

    def plan_of(self, uuid: str):
        return self.store.query_one(
            "SELECT p.* FROM recovery_plans p JOIN failures f ON f.id = p.failure_id"
            " WHERE f.session_uuid = ? ORDER BY p.id DESC LIMIT 1",
            (uuid,),
        )

    def fake_methods(self) -> list[str]:
        methods = []
        if self.fake_log.exists():
            for line in self.fake_log.read_text().splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry["dir"] == "recv" and isinstance(entry["msg"], dict):
                    methods.append(entry["msg"].get("method", ""))
        return methods


@pytest.fixture
def harness(store, fake_clock, fake_appserver_env, tmp_path):
    binary, scenario, log = fake_appserver_env
    config = load_config(env={}).config.with_overrides(
        codex_home=tmp_path / "codex-home",
        codex_binary=str(binary),
    )
    (tmp_path / "codex-home").mkdir(exist_ok=True)
    write_scenario(scenario)  # 默认剧本:正常额度
    return Harness(store, fake_clock, config, scenario, log)


def exhausted_turn(turn_id="t-1", start=60, end=120, **kw):
    return h.failed_turn(
        start=start,
        end=end,
        turn_id=turn_id,
        primary=h.window(100, 18000),
        secondary=h.window(40, 600000, minutes=h.WEEKLY),
        **kw,
    )


class TestDetectionToWaiting:
    def test_quota_failure_plans_window_reset(self, harness):
        """S-01/S-02:5h 100% → 检测 → 建计划 → WAITING_RECOVERY。"""

        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        harness.write(UUID_A, [h.session_meta(), *exhausted_turn()])

        result = harness.tick()
        assert harness.state_of(UUID_A) == "WAITING_RECOVERY"
        failure = harness.open_failure(UUID_A)
        assert failure["kind"] == "quota_five_hour"
        plan = harness.plan_of(UUID_A)
        assert plan["strategy"] == "window_reset"
        assert plan["not_before"] == h.T0 + 18000  # max(blocking resets_at)
        actions = result.sessions[0].actions
        assert any(a.startswith("failure_detected") for a in actions)
        assert any(a.startswith("plan_armed") for a in actions)

    def test_dual_window_reference_is_max(self, harness):
        """S-05:双耗尽 → not_before = max(五小时, 周窗)。"""

        harness.scenario_exhausted(weekly=WEEKLY_FULL)
        harness.register(UUID_A, "alpha")
        harness.write(
            UUID_A,
            [
                h.session_meta(),
                *h.failed_turn(
                    start=60,
                    end=120,
                    turn_id="t-1",
                    primary=h.window(100, 18000),
                    secondary=h.window(100, 600000, minutes=h.WEEKLY),
                ),
            ],
        )
        harness.tick()
        assert harness.open_failure(UUID_A)["kind"] == "quota_multi"
        assert harness.plan_of(UUID_A)["not_before"] == h.T0 + 600000

    def test_quota_unknown_goes_needs_human(self, harness):
        """S-20:无法归因 → manual_ack → NEEDS_HUMAN(fail-closed)。"""

        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        harness.write(
            UUID_A,
            [h.session_meta(), *h.failed_turn(start=60, end=120, turn_id="t-1")],
        )
        harness.tick()
        assert harness.state_of(UUID_A) == "NEEDS_HUMAN"
        assert harness.open_failure(UUID_A) is None
        kinds = [
            r["reason"] for r in harness.store.query("SELECT reason FROM state_history ORDER BY id")
        ]
        assert "unknown_quota_kind" in kinds

    def test_credits_failure_needs_human(self, harness):
        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        harness.write(
            UUID_A,
            [
                h.session_meta(),
                *h.failed_turn(
                    start=60,
                    end=120,
                    turn_id="t-1",
                    primary=h.window(100, 18000),
                    extra={"rate_limit_reached_type": "workspace_owner_credits_depleted"},
                ),
            ],
        )
        harness.tick()
        assert harness.state_of(UUID_A) == "NEEDS_HUMAN"

    def test_task_completion_is_not_failure(self, harness):
        harness.register(UUID_A, "alpha")
        harness.write(
            UUID_A, [h.session_meta(), *h.completed_turn(start=60, end=120, turn_id="t-9")]
        )
        result = harness.tick()
        assert harness.state_of(UUID_A) is None  # 无状态行 = ACTIVE 语义
        assert harness.store.query_one("SELECT COUNT(*) c FROM failures")["c"] == 0
        assert "task_completed" in result.sessions[0].actions


class TestRecoveryDecisions:
    def test_full_recovery_path_ready_decision(self, harness):
        """S-01 全链路(到 Phase 4 为止):检测 → 等待 → 三证据 → READY 决策。"""

        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        harness.write(UUID_A, [h.session_meta(), *exhausted_turn()])
        harness.tick()
        assert harness.state_of(UUID_A) == "WAITING_RECOVERY"

        harness.clock.advance(18001 + 60)
        harness.scenario_recovered()
        result = harness.tick()
        # 决策就绪但不投递(Phase 5 边界)
        decisions = [d for d in result.decisions if d["session"] == "alpha"]
        assert decisions and decisions[0]["ready"], decisions[0]["blocked_gates"]
        assert harness.state_of(UUID_A) == "WAITING_RECOVERY"  # 状态不前移
        # 无任何投递记录(投递执行器属 Phase 5)
        assert harness.store.query_one("SELECT COUNT(*) c FROM deliveries")["c"] == 0
        # 审计留痕 delivery_ready
        audit = harness.store.query(
            "SELECT * FROM audit_log WHERE category = 'delivery' AND summary LIKE '%ready%'"
        )
        assert audit

    def test_dual_window_partial_recovery_stays_blocked(self, harness):
        """S-05:双窗口,五小时先恢复、周窗未滚动 → 决策不就绪。"""

        harness.scenario_exhausted(weekly=WEEKLY_FULL)
        harness.register(UUID_A, "alpha")
        harness.write(
            UUID_A,
            [
                h.session_meta(),
                *h.failed_turn(
                    start=60,
                    end=120,
                    turn_id="t-1",
                    primary=h.window(100, 18000),
                    secondary=h.window(100, 600000, minutes=h.WEEKLY),
                ),
            ],
        )
        harness.tick()
        harness.clock.advance(600061)
        # 五小时恢复但周窗同窗口仍满
        harness.scenario_recovered(weekly=WEEKLY_FULL)
        result = harness.tick()
        decision = result.decisions[0]
        assert not decision["ready"]
        blocked = {g["gate"] for g in decision["blocked_gates"]}
        assert "G6" in blocked
        # 周窗滚动后就绪
        harness.clock.advance(60)
        harness.scenario_recovered(weekly=ROLLED_WEEKLY)
        result = harness.tick()
        assert result.decisions[0]["ready"]

    def test_official_early_reset_detected_before_not_before(self, harness):
        """S-06:官方提前重置(E2)→ 未到 not_before 即评估 → READY。"""

        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        harness.write(UUID_A, [h.session_meta(), *exhausted_turn()])
        harness.tick()
        not_before = harness.plan_of(UUID_A)["not_before"]

        harness.clock.advance(3600)  # 远未到 not_before(18000s 后)
        harness.scenario_recovered()
        result = harness.tick()
        assert harness.clock.epoch() < not_before
        signals = [s for s in result.early_signals if s["session"] == "alpha"]
        assert any(s["signal"] == "E2" for s in signals)
        decision = result.decisions[0]
        assert decision["ready"], decision["blocked_gates"]
        assert decision["signals"] == ["E2"]

    def test_voucher_redemption_observed_via_credits(self, harness):
        """S-07:手动兑换重置券 → E3 观察 + 窗口恢复;绝不调用 consume。"""

        harness.scenario_exhausted(
            credits={"availableCount": 1, "credits": [{"status": "available"}]}
        )
        harness.register(UUID_A, "alpha")
        harness.write(UUID_A, [h.session_meta(), *exhausted_turn()])
        harness.tick()  # 基线快照:available=1

        harness.clock.advance(1800)
        harness.scenario_recovered(
            credits={"availableCount": 0, "credits": [{"status": "redeemed"}]}
        )
        result = harness.tick()
        signals = [s["signal"] for s in result.early_signals if s["session"] == "alpha"]
        assert "E3" in signals
        assert result.decisions[0]["ready"]
        # T-GUARD-06:全程仅白名单只读方法
        methods = set(harness.fake_methods())
        assert methods <= {"initialize", "initialized", "account/rateLimits/read"}
        assert not any("consume" in m for m in methods)

    def test_jsonl_recovery_alone_never_ready(self, harness):
        """D-QM-6:实时不可用时,JSONL 显示恢复也不能作为证据。"""

        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        harness.write(UUID_A, [h.session_meta(), *exhausted_turn()])
        harness.tick()

        # App Server 实时查询失败(错误响应;常驻连接也随之判死)且 rollout
        # 后续样本显示恢复 —— 历史数据不得冒充实时证据
        write_scenario(
            harness.scenario,
            rate_limits_error={"code": -32000, "message": "simulated outage"},
        )
        harness.clock.advance(18001 + 60)
        harness.write(
            UUID_A,
            [
                h.turn_context(20000),
                h.task_started(20001, "t-2"),
                h.token_count(
                    20100,
                    primary=h.window(5, 36000),
                    secondary=h.window(40, 600000, minutes=h.WEEKLY),
                ),
            ],
        )
        result = harness.tick()
        decision = result.decisions[0]
        assert not decision["ready"]
        blocked = {g["gate"]: g["detail"] for g in decision["blocked_gates"]}
        assert blocked.get("G6") == "no_newer_appserver_snapshot"
        assert harness.state_of(UUID_A) == "WAITING_RECOVERY"

    def test_appserver_unavailable_backoff_and_unknown(self, harness):
        """S-08/S-18:App Server 不可用 → 错误快照、退避、评估不通过、不崩溃。"""

        write_scenario(harness.scenario, exit_before_handshake=True)
        harness.register(UUID_A, "alpha")
        harness.write(UUID_A, [h.session_meta(), *exhausted_turn()])
        harness.tick()
        assert harness.state_of(UUID_A) == "WAITING_RECOVERY"
        errors = harness.store.query(
            "SELECT * FROM quota_snapshots WHERE ok = 0 AND source = 'appserver'"
        )
        assert errors

        harness.clock.advance(60)
        result = harness.tick()  # 退避中:不再查询,但 tick 正常完成
        assert not result.restricted
        harness.clock.advance(300)
        result = harness.tick()
        decision = next((d for d in result.decisions if d["session"] == "alpha"), None)
        if decision is not None:
            assert not decision["ready"]


class TestHumanTakeover:
    def test_user_message_supersedes_waiting_plan(self, harness):
        """S-14/S-22:失败后人工消息 → SUPERSEDED → ACTIVE,计划取消。"""

        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        harness.write(UUID_A, [h.session_meta(), *exhausted_turn()])
        harness.tick()
        failure = harness.open_failure(UUID_A)

        harness.clock.advance(300)
        harness.write(UUID_A, [h.user_message(400, "I will handle it myself")])
        result = harness.tick()
        assert harness.state_of(UUID_A) == "ACTIVE"
        row = harness.store.query_one("SELECT status FROM failures WHERE id = ?", (failure["id"],))
        assert row["status"] == "superseded"
        assert harness.plan_of(UUID_A) is None or (
            harness.store.query_one(
                "SELECT p.status FROM recovery_plans p WHERE p.failure_id = ?",
                (failure["id"],),
            )["status"]
            == "cancelled"
        )
        assert "superseded" in result.sessions[0].actions

    def test_human_message_before_failure_ignored(self, harness):
        """失败轮次之前的历史用户消息不触发 supersede。"""

        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        harness.write(
            UUID_A,
            [h.session_meta(), h.user_message(10, "do something"), *exhausted_turn()],
        )
        harness.tick()
        assert harness.state_of(UUID_A) == "WAITING_RECOVERY"


class TestMultiSessionIsolation:
    def test_two_sessions_independent_states(self, harness):
        """S-10:同账户两会话,A 失败等待、B 正常;互不影响。"""

        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        harness.register(UUID_B, "beta")
        harness.write(UUID_A, [h.session_meta(), *exhausted_turn()])
        harness.write(UUID_B, [h.session_meta(), *h.completed_turn(start=60, end=90, turn_id="tb")])

        result = harness.tick()
        by_name = {s.name: s for s in result.sessions}
        assert harness.state_of(UUID_A) == "WAITING_RECOVERY"
        assert harness.state_of(UUID_B) is None
        assert by_name["beta"].faults == []

    def test_session_exception_isolated(self, harness, monkeypatch):
        """S-10:单会话处理抛异常 → 记 fault,其余会话照常。"""

        harness.register(UUID_A, "alpha")
        harness.register(UUID_B, "beta")
        harness.write(UUID_A, [h.session_meta()])
        harness.write(UUID_B, [h.session_meta()])

        scheduler = Scheduler(harness.store, harness.config, harness.clock)
        original = scheduler._process_session

        def broken(row, account, outcome, samples):
            if row["uuid"] == UUID_A:
                raise RuntimeError("boom")
            return original(row, account, outcome, samples)

        monkeypatch.setattr(scheduler, "_process_session", broken)
        result = scheduler.tick()
        by_name = {s.name: s for s in result.sessions}
        assert any("boom" in f for f in by_name["alpha"].faults)
        assert by_name["beta"].faults == []
        faults = harness.store.query("SELECT * FROM audit_log WHERE category = 'fault'")
        assert faults

    def test_shared_account_single_refresh(self, harness):
        """FR-02-A7:同账户多会话 → 每 tick 至多一次实时查询。"""

        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        harness.register(UUID_B, "beta")
        harness.write(UUID_A, [h.session_meta()])
        harness.write(UUID_B, [h.session_meta()])
        before = len(harness.fake_methods())
        harness.tick()
        after = len(harness.fake_methods())
        new_reads = sum(
            1 for m in harness.fake_methods()[before:after] if m == "account/rateLimits/read"
        )
        assert new_reads == 1


class TestPauseAndMissing:
    def test_session_pause_freezes_transitions(self, harness):
        """S-24:会话暂停 → 检测冻结;恢复后正常检测(不补投)。"""

        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        harness.write(UUID_A, [h.session_meta(), *exhausted_turn()])
        harness.store.execute("UPDATE sessions SET enabled = 0 WHERE uuid = ?", (UUID_A,))
        harness.tick()
        assert harness.state_of(UUID_A) is None  # 无迁移
        assert harness.store.query_one("SELECT COUNT(*) c FROM failures")["c"] == 0

        harness.store.execute("UPDATE sessions SET enabled = 1 WHERE uuid = ?", (UUID_A,))
        harness.clock.advance(60)
        harness.tick()
        assert harness.state_of(UUID_A) == "WAITING_RECOVERY"

    def test_global_pause_blocks_delivery_only(self, harness):
        """S-24:全局暂停 → 检测照常,投递 G8 冻结;解除后守门通过才投。"""

        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        harness.write(UUID_A, [h.session_meta(), *exhausted_turn()])
        harness.store.execute(
            "INSERT INTO runtime_flags(key, value, updated_at) VALUES ('global_pause', 'true', ?)",
            (harness.clock.epoch(),),
        )
        harness.tick()
        assert harness.state_of(UUID_A) == "WAITING_RECOVERY"  # 检测未被冻结

        harness.clock.advance(18061)
        harness.scenario_recovered()
        result = harness.tick()
        decision = result.decisions[0]
        assert not decision["ready"]
        assert any(g["gate"] == "G8" for g in decision["blocked_gates"])

        harness.store.execute("UPDATE runtime_flags SET value = 'false' WHERE key = 'global_pause'")
        harness.clock.advance(60)
        result = harness.tick()
        assert result.decisions[0]["ready"]

    def test_missing_then_grace_then_needs_human(self, harness):
        """S-15:rollout 消失 → MISSING → 宽限期后 NEEDS_HUMAN;绝不建进程。"""

        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        path = harness.write(UUID_A, [h.session_meta()])
        harness.tick()

        path.unlink()
        harness.clock.advance(60)
        harness.tick()
        assert harness.state_of(UUID_A) == "MISSING"

        harness.clock.advance(86401)
        harness.tick()
        assert harness.state_of(UUID_A) == "NEEDS_HUMAN"

    def test_missing_file_returns_restores_state(self, harness):
        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        path = harness.write(UUID_A, [h.session_meta(), *exhausted_turn()])
        harness.tick()
        assert harness.state_of(UUID_A) == "WAITING_RECOVERY"

        path.unlink()
        harness.clock.advance(60)
        harness.tick()
        assert harness.state_of(UUID_A) == "MISSING"

        harness.write(UUID_A, [])  # 文件回来
        harness.clock.advance(60)
        harness.tick()
        assert harness.state_of(UUID_A) == "WAITING_RECOVERY"  # 恢复到缺失前状态


class TestRestartPersistence:
    def test_scheduler_restart_preserves_state_and_dedups(self, harness):
        """服务器重启语义:新 Scheduler 实例 + 游标重建 → 状态延续、失败不重复。"""

        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        harness.write(UUID_A, [h.session_meta(), *exhausted_turn()])
        harness.tick()
        assert harness.state_of(UUID_A) == "WAITING_RECOVERY"

        harness.tick(fresh_scheduler=True)
        assert harness.state_of(UUID_A) == "WAITING_RECOVERY"
        assert harness.store.query_one("SELECT COUNT(*) c FROM failures")["c"] == 1

    def test_crash_residual_failure_detected_replanned(self, harness):
        """崩溃残留:FAILURE_DETECTED 且无计划 → 下一 tick 补建计划。"""

        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        harness.write(UUID_A, [h.session_meta(), *exhausted_turn()])
        harness.tick()
        # 模拟建计划前崩溃:回退状态与计划
        harness.store.execute("DELETE FROM recovery_plans")
        harness.store.execute("DELETE FROM state_history WHERE reason = 'plan_armed'")
        harness.store.execute(
            "UPDATE session_state SET state = 'FAILURE_DETECTED' WHERE session_uuid = ?",
            (UUID_A,),
        )
        harness.store.execute(
            "UPDATE failures SET status = 'open' WHERE session_uuid = ?", (UUID_A,)
        )
        harness.clock.advance(60)
        result = harness.tick(fresh_scheduler=True)
        assert harness.state_of(UUID_A) == "WAITING_RECOVERY"
        assert harness.plan_of(UUID_A) is not None
        assert any("plan_armed:residual" in a for a in result.sessions[0].actions)


class TestSweep:
    def test_attempting_delivery_swept_to_unknown(self, harness):
        """S-16/FR-08-A7(启动清扫侧):attempting → unknown + NEEDS_HUMAN。"""

        harness.register(UUID_A, "alpha")
        harness.write(UUID_A, [h.session_meta()])
        harness.tick()  # 初始化 sweep 状态
        # 直接注入 attempting 残留(模拟崩溃)
        cursor = harness.store.execute(
            "INSERT INTO failures(session_uuid, turn_id, kind, detected_at, evidence,"
            " blocking_window_keys, status) VALUES (?, 'tx', 'quota_five_hour', ?, '{}', '[]',"
            " 'delivering')",
            (UUID_A, harness.clock.epoch()),
        )
        harness.store.execute(
            "INSERT INTO deliveries(failure_id, attempt_no, argv, started_at, outcome)"
            " VALUES (?, 1, '[]', ?, 'attempting')",
            (cursor.lastrowid, harness.clock.epoch()),
        )
        harness.tick(fresh_scheduler=True)
        delivery = harness.store.query_one("SELECT * FROM deliveries")
        assert delivery["outcome"] == "unknown"
        assert harness.state_of(UUID_A) == "NEEDS_HUMAN"

    def test_orphan_plan_expired(self, harness):
        harness.register(UUID_A, "alpha")
        harness.write(UUID_A, [h.session_meta()])
        harness.tick()
        cursor = harness.store.execute(
            "INSERT INTO failures(session_uuid, turn_id, kind, detected_at, evidence,"
            " blocking_window_keys, status) VALUES (?, 'tx', 'quota_five_hour', ?, '{}', '[]',"
            " 'superseded')",
            (UUID_A, harness.clock.epoch()),
        )
        harness.store.execute(
            "INSERT INTO recovery_plans(failure_id, strategy, not_before, basis, status,"
            " created_at, updated_at) VALUES (?, 'window_reset', 0, '{}', 'waiting', ?, ?)",
            (cursor.lastrowid, harness.clock.epoch(), harness.clock.epoch()),
        )
        harness.tick(fresh_scheduler=True)
        plan = harness.store.query_one("SELECT * FROM recovery_plans")
        assert plan["status"] == "expired"


class TestCheckNoDeliver:
    def test_check_no_deliver_zero_side_effects(self, harness):
        """check --no-deliver:纯只读评估,零投递副作用(FR-06-A3/T-P4)。"""

        harness.scenario_exhausted()
        harness.register(UUID_A, "alpha")
        harness.write(UUID_A, [h.session_meta(), *exhausted_turn()])
        harness.tick()
        harness.clock.advance(18061)
        harness.scenario_recovered()

        before_deliveries = harness.store.query_one("SELECT COUNT(*) c FROM deliveries")["c"]
        before_log_len = len(harness.fake_methods())
        result = harness.tick(force_refresh=True)

        assert harness.store.query_one("SELECT COUNT(*) c FROM deliveries")["c"] == (
            before_deliveries  # 仍为 0
        )
        # 无 DELIVERING/DELIVERED 状态
        states = {r["state"] for r in harness.store.query("SELECT state FROM session_state")}
        assert not states & {"DELIVERING", "DELIVERED_UNCONFIRMED"}
        # Fake app-server 全程只见白名单只读方法(T-GUARD-06)
        methods = set(harness.fake_methods())
        assert methods <= {"initialize", "initialized", "account/rateLimits/read"}
        assert len(harness.fake_methods()) > before_log_len  # 确有查询发生
        decision = result.decisions[0]
        assert decision["ready"]  # 评估本身照常得出结论
