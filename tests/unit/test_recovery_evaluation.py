"""P4 恢复评估器单元测试:G6 三证据 + G1..G12 守门 + 模板渲染。

对应设计:quota-monitoring §5.2(D-QM-2/D-QM-6)、queue-safety §2。
核心红线:
- JSONL 历史快照永远不是恢复证据(D-QM-6);
- 证据必须新鲜(appserver 且非 stale);
- manual_ack 类(credits/spend_control/unknown)恒不通过;
- 任何守门"无法评估"同样不通过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autopilot.config import load_config
from codex_autopilot.quota.model import QuotaThresholds
from codex_autopilot.quota.parser import parse_appserver_response
from codex_autopilot.recovery.evaluation import (
    MAX_TEMPLATE_CHARS,
    RecoveryEvaluator,
    render_template,
)
from codex_autopilot.store.dao import AccountDao, NewQuotaSnapshot, NewQuotaWindow, QuotaSnapshotDao
from tests import helpers as h

UUID = "01234567-89ab-cdef-0123-456789abcdef"


@pytest.fixture
def config(tmp_path):
    return load_config(env={}).config.with_overrides(codex_home=tmp_path / "codex-home")


@pytest.fixture
def evaluator(store, fake_clock, config):
    return RecoveryEvaluator(store, config, fake_clock)


def register_session(store, fake_clock, tmp_path) -> tuple[str, int]:
    """注册会话并建 rollout 文件;返回 (codex_home, account_id)。"""

    home = str(tmp_path / "home")
    with store.transaction():
        account = AccountDao(store).get_or_create(home, label=None, now=fake_clock.epoch())
    store.execute(
        "INSERT INTO sessions(uuid, name, project_path, account_id, enabled, auto_resume,"
        " created_at, updated_at) VALUES (?, 'alpha', '/tmp/project', ?, 1, 1, ?, ?)",
        (UUID, int(account["id"]), fake_clock.epoch(), fake_clock.epoch()),
    )
    write_rollout(home)
    return home, int(account["id"])


def write_rollout(home: str) -> None:
    directory = Path(home) / "sessions" / "2026" / "01" / "01"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"rollout-2026-01-01T00-00-00-{UUID}.jsonl").write_text(
        h.file_content([h.session_meta()]), encoding="utf-8"
    )


def insert_failure(
    store,
    fake_clock,
    *,
    turn: str = "t-1",
    kind: str = "quota_five_hour",
    blocking=("300",),
    baseline_windows: list[dict] | None = None,
    sample_ts: int | None = h.T0 + 60,
) -> int:
    windows = (
        baseline_windows
        if baseline_windows is not None
        else [
            {
                "bucket_key": "300",
                "window_kind": "five_hour",
                "used_percent": 100.0,
                "resets_at": h.T0 + 18000,
                "window_minutes": 300,
            }
        ]
    )
    evidence = {"source": "jsonl", "windows": windows, "snapshot_captured_at": sample_ts}
    cursor = store.execute(
        "INSERT INTO failures(session_uuid, turn_id, kind, detected_at, evidence,"
        " blocking_window_keys, status) VALUES (?, ?, ?, ?, ?, ?, 'planned')",
        (UUID, turn, kind, fake_clock.epoch(), json.dumps(evidence), json.dumps(list(blocking))),
    )
    return int(cursor.lastrowid)


def insert_appserver_snapshot(
    store,
    fake_clock,
    account_id: int,
    *,
    primary: dict | None = None,
    secondary: dict | None = None,
    ordinary: bool | None = True,
    captured_at: int,
) -> None:
    response = {
        "rateLimitsByLimitId": {
            "codex": {
                "primary": primary
                or {"usedPercent": 100, "resetsAt": h.T0 + 18000, "windowDurationMins": 300},
                "secondary": secondary
                or {"usedPercent": 40, "resetsAt": h.T0 + 700000, "windowDurationMins": 10080},
            }
        },
        "ordinaryUsageAllowed": ordinary,
    }
    snapshot = parse_appserver_response(
        response, thresholds=QuotaThresholds(), captured_at=captured_at
    )
    QuotaSnapshotDao(store).insert(
        account_id,
        NewQuotaSnapshot(
            source="appserver",
            captured_at=captured_at,
            ok=snapshot.ok,
            ordinary_usage_allowed=snapshot.ordinary_usage_allowed,
            spend_control_reached=snapshot.spend_control_reached,
            rate_limit_reached_type=snapshot.rate_limit_reached_type,
            raw=json.dumps(response),
            parser_version=snapshot.parser_version,
            windows=tuple(
                NewQuotaWindow(
                    bucket_key=w.bucket_key,
                    window_kind=w.window_kind,
                    kind_inferred=w.kind_inferred,
                    used_percent=w.used_percent,
                    resets_at=w.resets_at,
                    window_minutes=w.window_minutes,
                    is_exhausted=w.is_exhausted,
                )
                for w in snapshot.windows
            ),
        ),
        ingested_at=fake_clock.epoch(),
    )


class TestG6ThreeEvidence:
    def test_rolled_window_fresh_appserver_ordinary_true_passes(
        self, store, fake_clock, evaluator, tmp_path
    ):
        _, account_id = register_session(store, fake_clock, tmp_path)
        failure_id = insert_failure(store, fake_clock)
        insert_appserver_snapshot(
            store,
            fake_clock,
            account_id,
            primary={"usedPercent": 5, "resetsAt": h.T0 + 36000, "windowDurationMins": 300},
            captured_at=h.T0 + 20000,
        )
        fake_clock.advance(20100)
        failure = store.query_one("SELECT * FROM failures WHERE id = ?", (failure_id,))
        passed, reason, evidence = evaluator.recovery_evidence(failure, account_id)
        assert passed, reason
        assert reason == "three_evidence_ok"
        assert evidence is not None and evidence.ordinary_usage_allowed is True

    def test_no_newer_appserver_snapshot_fails(self, store, fake_clock, evaluator, tmp_path):
        _, account_id = register_session(store, fake_clock, tmp_path)
        failure_id = insert_failure(store, fake_clock)
        fake_clock.advance(100)
        failure = store.query_one("SELECT * FROM failures WHERE id = ?", (failure_id,))
        passed, reason, _ = evaluator.recovery_evidence(failure, account_id)
        assert not passed and reason == "no_newer_appserver_snapshot"

    def test_stale_evidence_fails(self, store, fake_clock, evaluator, tmp_path):
        """证据过期:实时额度不可用时不得放行(哪怕此前快照显示恢复)。"""

        _, account_id = register_session(store, fake_clock, tmp_path)
        failure_id = insert_failure(store, fake_clock)
        insert_appserver_snapshot(
            store,
            fake_clock,
            account_id,
            primary={"usedPercent": 5, "resetsAt": h.T0 + 36000, "windowDurationMins": 300},
            captured_at=h.T0 + 20000,
        )
        fake_clock.advance(20000 + 601)  # fresh_ttl 600s 之外
        failure = store.query_one("SELECT * FROM failures WHERE id = ?", (failure_id,))
        passed, reason, _ = evaluator.recovery_evidence(failure, account_id)
        assert not passed and reason == "evidence_stale"

    def test_jsonl_snapshot_is_never_evidence(self, store, fake_clock, evaluator, tmp_path):
        """D-QM-6:JSONL 历史快照显示恢复也不算证据。"""

        _, account_id = register_session(store, fake_clock, tmp_path)
        failure_id = insert_failure(store, fake_clock)
        QuotaSnapshotDao(store).insert(
            account_id,
            NewQuotaSnapshot(
                source="jsonl",
                captured_at=h.T0 + 30000,
                ok=True,
                windows=(
                    NewQuotaWindow(
                        bucket_key="300",
                        window_kind="five_hour",
                        used_percent=5.0,
                        resets_at=h.T0 + 36000,
                        window_minutes=300,
                    ),
                ),
            ),
            ingested_at=fake_clock.epoch(),
        )
        fake_clock.advance(30100)
        failure = store.query_one("SELECT * FROM failures WHERE id = ?", (failure_id,))
        passed, reason, _ = evaluator.recovery_evidence(failure, account_id)
        assert not passed and reason == "no_newer_appserver_snapshot"

    def test_ordinary_false_fails(self, store, fake_clock, evaluator, tmp_path):
        _, account_id = register_session(store, fake_clock, tmp_path)
        failure_id = insert_failure(store, fake_clock)
        insert_appserver_snapshot(
            store,
            fake_clock,
            account_id,
            primary={"usedPercent": 5, "resetsAt": h.T0 + 36000, "windowDurationMins": 300},
            ordinary=False,
            captured_at=h.T0 + 20000,
        )
        fake_clock.advance(20100)
        failure = store.query_one("SELECT * FROM failures WHERE id = ?", (failure_id,))
        passed, reason, _ = evaluator.recovery_evidence(failure, account_id)
        assert not passed and reason == "ordinary_usage_not_allowed"

    def test_window_not_rolled_and_not_below_resume_max_fails(
        self, store, fake_clock, evaluator, tmp_path
    ):
        """同窗口未滚动且 91% ≥ resume_max(90):不算解除。"""

        _, account_id = register_session(store, fake_clock, tmp_path)
        failure_id = insert_failure(store, fake_clock)
        insert_appserver_snapshot(
            store,
            fake_clock,
            account_id,
            primary={"usedPercent": 91, "resetsAt": h.T0 + 18000, "windowDurationMins": 300},
            captured_at=h.T0 + 20000,
        )
        fake_clock.advance(20100)
        failure = store.query_one("SELECT * FROM failures WHERE id = ?", (failure_id,))
        passed, reason, _ = evaluator.recovery_evidence(failure, account_id)
        assert not passed and reason == "window_not_cleared"

    def test_manual_ack_kinds_never_pass(self, store, fake_clock, evaluator, tmp_path):
        _, account_id = register_session(store, fake_clock, tmp_path)
        insert_appserver_snapshot(
            store,
            fake_clock,
            account_id,
            primary={"usedPercent": 5, "resetsAt": h.T0 + 99999, "windowDurationMins": 300},
            captured_at=h.T0 + 20000,
        )
        fake_clock.advance(100)
        for index, kind in enumerate(("quota_credits", "quota_spend_control", "quota_unknown")):
            failure_id = insert_failure(
                store,
                fake_clock,
                turn=f"t-{index}",
                kind=kind,
                blocking=(),
                baseline_windows=[],
            )
            failure = store.query_one("SELECT * FROM failures WHERE id = ?", (failure_id,))
            passed, reason, _ = evaluator.recovery_evidence(failure, account_id)
            assert not passed and reason == "manual_ack_kind"

    def test_dual_window_requires_all_cleared(self, store, fake_clock, evaluator, tmp_path):
        """S-05:双窗口失败,一窗解除不通过,全解除才通过。"""

        _, account_id = register_session(store, fake_clock, tmp_path)
        failure_id = insert_failure(
            store,
            fake_clock,
            kind="quota_multi",
            blocking=("300", "10080"),
            baseline_windows=[
                {
                    "bucket_key": "300",
                    "window_kind": "five_hour",
                    "used_percent": 100.0,
                    "resets_at": h.T0 + 18000,
                    "window_minutes": 300,
                },
                {
                    "bucket_key": "10080",
                    "window_kind": "weekly",
                    "used_percent": 100.0,
                    "resets_at": h.T0 + 700000,
                    "window_minutes": 10080,
                },
            ],
        )
        insert_appserver_snapshot(
            store,
            fake_clock,
            account_id,
            primary={"usedPercent": 5, "resetsAt": h.T0 + 36000, "windowDurationMins": 300},
            secondary={"usedPercent": 100, "resetsAt": h.T0 + 700000, "windowDurationMins": 10080},
            captured_at=h.T0 + 20000,
        )
        fake_clock.advance(20100)
        failure = store.query_one("SELECT * FROM failures WHERE id = ?", (failure_id,))
        passed, reason, _ = evaluator.recovery_evidence(failure, account_id)
        assert not passed and reason == "window_not_cleared"
        # 周窗也滚动后通过
        insert_appserver_snapshot(
            store,
            fake_clock,
            account_id,
            primary={"usedPercent": 5, "resetsAt": h.T0 + 36000, "windowDurationMins": 300},
            secondary={"usedPercent": 10, "resetsAt": h.T0 + 800000, "windowDurationMins": 10080},
            captured_at=h.T0 + 21000,
        )
        fake_clock.advance(100)
        failure = store.query_one("SELECT * FROM failures WHERE id = ?", (failure_id,))
        passed, reason, _ = evaluator.recovery_evidence(failure, account_id)
        assert passed, reason


class TestGates:
    def _prepare(self, store, fake_clock, tmp_path, **evidence_kwargs):
        _, account_id = register_session(store, fake_clock, tmp_path)
        failure_id = insert_failure(store, fake_clock)
        insert_appserver_snapshot(
            store,
            fake_clock,
            account_id,
            primary={"usedPercent": 5, "resetsAt": h.T0 + 36000, "windowDurationMins": 300},
            captured_at=h.T0 + 20000,
            **evidence_kwargs,
        )
        fake_clock.advance(20100)
        session = store.query_one("SELECT * FROM sessions WHERE uuid = ?", (UUID,))
        failure = store.query_one("SELECT * FROM failures WHERE id = ?", (failure_id,))
        return session, failure

    def _evaluate(self, store, fake_clock, tmp_path, evaluator, **kwargs):
        session, failure = self._prepare(store, fake_clock, tmp_path)
        defaults = {
            "global_pause": False,
            "lock_held": True,
            "integrity_ok": True,
            "human_messages": (),
            "has_open_turn": False,
        }
        defaults.update(kwargs)
        return evaluator.evaluate(session, failure, None, **defaults)

    def test_all_gates_pass_when_recovered(self, store, fake_clock, tmp_path, evaluator):
        decision = self._evaluate(store, fake_clock, tmp_path, evaluator)
        assert decision.ready, [g.detail for g in decision.gates]
        statuses = {g.gate: g.status for g in decision.gates}
        assert set(statuses) == {f"G{i}" for i in range(1, 13)}

    def test_g8_auto_resume_off(self, store, fake_clock, tmp_path, evaluator):
        session, failure = self._prepare(store, fake_clock, tmp_path)
        store.execute("UPDATE sessions SET auto_resume = 0 WHERE uuid = ?", (UUID,))
        session = store.query_one("SELECT * FROM sessions WHERE uuid = ?", (UUID,))
        decision = evaluator.evaluate(
            session, failure, None, global_pause=False, lock_held=True, integrity_ok=True
        )
        assert not decision.ready
        assert decision.gate("G8").status == "fail"
        assert "auto_resume off" in decision.gate("G8").detail

    def test_g8_global_pause(self, store, fake_clock, tmp_path, evaluator):
        session, failure = self._prepare(store, fake_clock, tmp_path)
        decision = evaluator.evaluate(
            session, failure, None, global_pause=True, lock_held=True, integrity_ok=True
        )
        assert "global pause" in decision.gate("G8").detail

    def test_g3_human_message_fails(self, store, fake_clock, tmp_path, evaluator):
        from codex_autopilot.recovery.model import HumanMessage

        session, failure = self._prepare(store, fake_clock, tmp_path)
        decision = evaluator.evaluate(
            session,
            failure,
            None,
            global_pause=False,
            lock_held=True,
            integrity_ok=True,
            human_messages=(HumanMessage(timestamp=h.T0 + 100, head="manual"),),
        )
        assert decision.gate("G3").status == "fail"

    def test_g7_open_turn_or_unknown_defers(self, store, fake_clock, tmp_path, evaluator):
        session, failure = self._prepare(store, fake_clock, tmp_path)
        decision = evaluator.evaluate(
            session,
            failure,
            None,
            global_pause=False,
            lock_held=True,
            integrity_ok=True,
            has_open_turn=True,
        )
        assert decision.gate("G7").status == "fail"
        decision = evaluator.evaluate(
            session,
            failure,
            None,
            global_pause=False,
            lock_held=True,
            integrity_ok=True,
            has_open_turn=None,
        )
        assert decision.gate("G7").status == "fail"  # 不确定视为在跑

    def test_g9_lock_not_held_cannot_evaluate(self, store, fake_clock, tmp_path, evaluator):
        session, failure = self._prepare(store, fake_clock, tmp_path)
        decision = evaluator.evaluate(
            session, failure, None, global_pause=False, lock_held=False, integrity_ok=True
        )
        assert decision.gate("G9").status == "cannot_evaluate"
        assert not decision.ready

    def test_g1_closed_failure_fails(self, store, fake_clock, tmp_path, evaluator):
        session, failure = self._prepare(store, fake_clock, tmp_path)
        store.execute("UPDATE failures SET status = 'superseded' WHERE id = ?", (failure["id"],))
        failure = store.query_one("SELECT * FROM failures WHERE id = ?", (failure["id"],))
        decision = evaluator.evaluate(
            session, failure, None, global_pause=False, lock_held=True, integrity_ok=True
        )
        assert decision.gate("G1").status == "fail"

    def test_g10_bad_session_template_fails(self, store, fake_clock, tmp_path, evaluator):
        session, failure = self._prepare(store, fake_clock, tmp_path)
        store.execute(
            "UPDATE sessions SET resume_template = 'leak {prompt}' WHERE uuid = ?", (UUID,)
        )
        session = store.query_one("SELECT * FROM sessions WHERE uuid = ?", (UUID,))
        decision = evaluator.evaluate(
            session, failure, None, global_pause=False, lock_held=True, integrity_ok=True
        )
        assert decision.gate("G10").status == "fail"
        assert "unknown placeholder" in decision.gate("G10").detail


class TestTemplate:
    def test_default_template_renders(self):
        text, error = render_template(
            "Continue the previous task automatically. (codex-autopilot resume #{failure_id})",
            session_name="alpha",
            failure_kind="quota_five_hour",
            failure_id=7,
            detected_at_local="2026-01-01 00:00:00",
        )
        assert error is None
        assert "#7" in text

    def test_unknown_placeholder_rejected(self):
        _, error = render_template(
            "leak {prompt}",
            session_name="a",
            failure_kind="k",
            failure_id=1,
            detected_at_local="x",
        )
        assert error and "unknown placeholder" in error

    def test_control_characters_rejected(self):
        _, error = render_template(
            "bad \x01 template",
            session_name="a",
            failure_kind="k",
            failure_id=1,
            detected_at_local="x",
        )
        assert error and "control characters" in error

    def test_too_long_rejected(self):
        _, error = render_template(
            "x" * (MAX_TEMPLATE_CHARS + 1),
            session_name="a",
            failure_kind="k",
            failure_id=1,
            detected_at_local="x",
        )
        assert error and "exceeds" in error

    def test_empty_rejected(self):
        _, error = render_template(
            "   ",
            session_name="a",
            failure_kind="k",
            failure_id=1,
            detected_at_local="x",
        )
        assert error and "empty" in error
