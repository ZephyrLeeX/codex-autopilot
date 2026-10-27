"""P4 CLI 集成测试:list --codex-sessions / status / history / check / tick / pause / resume。

L1 红线:codex 二进制一律指向 Fake(conftest 隔离环境 + CODEX__BINARY 覆盖)。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from tests import helpers as h
from tests.conftest import write_scenario

UUID_A = "01234567-89ab-cdef-0123-456789abcdef"
UUID_B = "fedcba98-7654-3210-fedc-ba9876543210"
UUID_C = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def fake_binary(monkeypatch, fake_appserver_env):
    binary, scenario, log = fake_appserver_env
    monkeypatch.setenv("CODEX_AUTOPILOT_CODEX__BINARY", str(binary))
    return binary, scenario, log


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    (home / "sessions" / "2026" / "01" / "01").mkdir(parents=True)
    monkeypatch.setenv("CODEX_AUTOPILOT_CODEX__CODEX_HOME", str(home))
    return home


def add(cli, *args, uuid=UUID_A, name="alpha", home=None, extra=()) -> object:
    return cli(
        "add",
        "--session",
        uuid,
        "--name",
        name,
        "--project",
        "/tmp",
        "--auto-resume",
        *extra,
    )


def write_rollout(home: Path, uuid: str, lines: list[str], *, ts="2026-01-01T00-00-00") -> Path:
    directory = home / "sessions" / "2026" / "01" / "01"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-{ts}-{uuid}.jsonl"
    path.write_text(h.file_content(lines), encoding="utf-8")
    return path


def exhausted_turn(turn_id="t-1", start=60, end=120):
    return h.failed_turn(
        start=start,
        end=end,
        turn_id=turn_id,
        primary=h.window(100, 18000),
        secondary=h.window(40, 600000, minutes=h.WEEKLY),
    )


def scenario_exhausted(scenario):
    write_scenario(
        scenario,
        rate_limits_result={
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "usedPercent": 100,
                        "resetsAt": h.T0 + 18000,
                        "windowDurationMins": 300,
                    },
                    "secondary": {
                        "usedPercent": 40,
                        "resetsAt": h.T0 + 600000,
                        "windowDurationMins": 10080,
                    },
                }
            },
            "ordinaryUsageAllowed": True,
        },
    )


def scenario_recovered(scenario):
    write_scenario(
        scenario,
        rate_limits_result={
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "usedPercent": 5,
                        "resetsAt": h.T0 + 36000,
                        "windowDurationMins": 300,
                    },
                    "secondary": {
                        "usedPercent": 40,
                        "resetsAt": h.T0 + 600000,
                        "windowDurationMins": 10080,
                    },
                }
            },
            "ordinaryUsageAllowed": True,
        },
    )


class TestListCodexSessions:
    def test_scan_reports_sessions_and_flags(self, cli, home, tmp_path):
        write_rollout(home, UUID_A, [h.session_meta(cwd="/work/proj-a"), *exhausted_turn()])
        write_rollout(home, UUID_B, [h.session_meta(cwd="/work/proj-b")])
        # 分裂链:注册表视角的 B 有续链文件
        write_rollout(
            home,
            f"{UUID_B}_{UUID_C}",
            [h.token_count(3600, primary=h.window(10, 36000))],
            ts="2026-01-01T01-00-00",
        )
        # 非 rollout 文件应被忽略
        (home / "sessions" / "2026" / "01" / "01" / "session_index.jsonl").write_text("{}\n")
        # thread catalog 交叉(codex.db 只读)
        db = home / "sqlite" / "codex.db"
        db.parent.mkdir(exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE local_thread_catalog (thread_uuid TEXT, updated_at INTEGER)")
        conn.execute("INSERT INTO local_thread_catalog VALUES (?, 1)", (UUID_A,))
        conn.commit()
        conn.close()

        result = cli("--json", "list", "--codex-sessions", "--days", "400")
        assert result.exit_code == 0, result.output
        rows = json.loads(result.stdout)
        by_uuid = {row["uuid"]: row for row in rows}
        # 链尾 = 文件自身会话:B_C 文件归 C(祖先 B 仍在 B 自己的文件里)
        assert set(by_uuid) == {UUID_A, UUID_B, UUID_C}
        row_a = by_uuid[UUID_A]
        assert row_a["cwd"] == "/work/proj-a"
        assert row_a["line_count"] > 0
        assert row_a["registered"] is False
        assert row_a["in_thread_catalog"] is True
        row_b = by_uuid[UUID_B]
        assert row_b["in_thread_catalog"] is False
        assert row_b["chain"] == [UUID_B]
        assert by_uuid[UUID_C]["chain"] == [UUID_B, UUID_C]  # 祖先链展示

    def test_registered_flag(self, cli, home):
        write_rollout(home, UUID_A, [h.session_meta()])
        add(cli, uuid=UUID_A, name="alpha")
        result = cli("--json", "list", "--codex-sessions", "--days", "400")
        rows = json.loads(result.stdout)
        assert rows[0]["uuid"] == UUID_A and rows[0]["registered"] is True

    def test_days_filter(self, cli, home):
        write_rollout(home, UUID_A, [h.session_meta()], ts="2025-01-01T00-00-00")
        result = cli("--json", "list", "--codex-sessions", "--days", "1")
        assert result.exit_code == 0
        assert json.loads(result.stdout) == []

    def test_archived_included_and_flagged(self, cli, home):
        directory = home / "archived_sessions" / "2026" / "01" / "01"
        directory.mkdir(parents=True)
        (directory / f"rollout-2026-01-01T00-00-00-{UUID_A}.jsonl").write_text(
            h.file_content([h.session_meta()]), encoding="utf-8"
        )
        result = cli("--json", "list", "--codex-sessions", "--days", "400")
        rows = json.loads(result.stdout)
        assert rows and rows[0]["archived"] is True

    def test_human_output(self, cli, home):
        write_rollout(home, UUID_A, [h.session_meta(cwd="/work/a"), *exhausted_turn()])
        result = cli("list", "--codex-sessions", "--days", "400")
        assert result.exit_code == 0
        assert UUID_A in result.output
        assert "UUID" in result.output


class TestTickCommand:
    def test_tick_reports_cycle(self, cli, fake_binary, home):
        write_rollout(home, UUID_A, [h.session_meta(), *exhausted_turn()])
        add(cli, uuid=UUID_A, name="alpha")
        scenario_exhausted(fake_binary[1])
        result = cli("tick")
        assert result.exit_code == 0, result.output
        assert "failure_detected" in result.output

    def test_tick_json_schema(self, cli, fake_binary, home):
        write_rollout(home, UUID_A, [h.session_meta(), *exhausted_turn()])
        add(cli, uuid=UUID_A, name="alpha")
        scenario_exhausted(fake_binary[1])
        result = cli("--json", "tick")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert set(payload) >= {
            "started_at",
            "finished_at",
            "restricted",
            "global_pause",
            "sessions",
            "decisions",
        }

    def test_tick_exit_21_when_lock_held(self, cli, fake_binary, home, tmp_path):
        """S-17:另一实例持锁 → 退出码 21。"""

        from codex_autopilot.scheduling.single_instance import SingleInstanceLock

        # 先以独立 state_dir 运行一次 tick,让配置/DB 就绪
        add(cli, uuid=UUID_A, name="alpha")
        state_dir = json.loads(cli("--json", "list").output)  # 只为触发 DB 创建(占位)
        del state_dir
        xdg_state = Path(os.environ["XDG_STATE_HOME"]) / "codex-autopilot"
        lock = SingleInstanceLock(xdg_state / "daemon.lock")
        lock.acquire()
        try:
            result = cli("tick")
            assert result.exit_code == 21
        finally:
            lock.release()


class TestPauseResume:
    def test_global_pause_roundtrip(self, cli, home):
        add(cli, uuid=UUID_A, name="alpha")
        assert cli("pause").exit_code == 0
        result = cli("--json", "status")
        assert json.loads(result.stdout)["global_pause"] is True
        assert cli("resume").exit_code == 0
        assert json.loads(cli("--json", "status").output)["global_pause"] is False

    def test_session_pause_and_resume(self, cli, home):
        write_rollout(home, UUID_A, [h.session_meta()])
        add(cli, uuid=UUID_A, name="alpha")
        assert cli("pause", "--session", "alpha").exit_code == 0
        listing = json.loads(cli("--json", "list").output)
        assert listing[0]["enabled"] is False
        assert cli("resume", "--session", "alpha").exit_code == 0
        assert json.loads(cli("--json", "list").output)[0]["enabled"] is True

    def test_needs_human_requires_ack(self, cli, fake_binary, home):
        write_rollout(home, UUID_A, [h.session_meta(), *exhausted_turn()])
        add(cli, uuid=UUID_A, name="alpha")
        # 触发 needs_human:无样本失败(quota_unknown)
        path = (
            home / "sessions" / "2026" / "01" / "01" / f"rollout-2026-01-01T00-00-00-{UUID_A}.jsonl"
        )
        path.write_text(
            h.file_content([h.session_meta(), *h.failed_turn(start=60, end=120, turn_id="t-1")]),
            encoding="utf-8",
        )
        cli("tick")
        result = cli("--json", "status", "alpha")
        assert json.loads(result.stdout)["sessions"][0]["state"] == "NEEDS_HUMAN"
        assert cli("status", "alpha").exit_code == 10
        # resume 无 ack → 用法错误
        denied = cli("resume", "--session", "alpha")
        assert denied.exit_code == 2
        # 显式 ack → 回 ACTIVE
        allowed = cli("resume", "--session", "alpha", "--ack-needs-human")
        assert allowed.exit_code == 0, allowed.output
        assert (
            json.loads(cli("--json", "status", "alpha").output)["sessions"][0]["state"] == "ACTIVE"
        )


class TestStatusCommand:
    def test_status_shows_failure_and_plan(self, cli, fake_binary, home):
        write_rollout(home, UUID_A, [h.session_meta(), *exhausted_turn()])
        add(cli, uuid=UUID_A, name="alpha")
        scenario_exhausted(fake_binary[1])
        cli("tick")
        result = cli("--json", "status", "alpha")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        session = payload["sessions"][0]
        assert session["state"] == "WAITING_RECOVERY"
        assert session["failure"]["kind"] == "quota_five_hour"
        assert session["plan"]["strategy"] == "window_reset"
        # CLI 用真实时钟:fixture 的 resets_at(2026-01)已过期 → not_before=now
        assert session["plan"]["not_before"] <= session["failure"]["detected_at"] + 5
        # 人读模式
        human = cli("status", "alpha")
        assert "WAITING_RECOVERY" in human.output
        assert "quota_five_hour" in human.output


class TestHistoryCommand:
    def test_history_lists_events(self, cli, fake_binary, home):
        write_rollout(home, UUID_A, [h.session_meta(), *exhausted_turn()])
        add(cli, uuid=UUID_A, name="alpha")
        scenario_exhausted(fake_binary[1])
        cli("tick")
        result = cli("--json", "history", "--session", "alpha")
        events = json.loads(result.stdout)
        types = [event["type"] for event in events]
        assert "failure" in types and "plan" in types and "transition" in types

    def test_history_since_filter(self, cli, fake_binary, home):
        write_rollout(home, UUID_A, [h.session_meta(), *exhausted_turn()])
        add(cli, uuid=UUID_A, name="alpha")
        scenario_exhausted(fake_binary[1])
        cli("tick")
        result = cli("--json", "history", "--since", "2030-01-01")
        assert json.loads(result.stdout) == []
        result = cli("--json", "history", "--since", "2020-01-01")
        assert json.loads(result.stdout)

    def test_history_invalid_since(self, cli, fake_binary, home):
        add(cli, uuid=UUID_A, name="alpha")
        result = cli("history", "--since", "not-a-date")
        assert result.exit_code == 2


class TestCheckCommand:
    def test_check_blocked_exit_11(self, cli, fake_binary, home):
        """额度仍阻塞:窗口未滚动/未降 → G6 不通过 → 退出码 11。"""

        write_rollout(home, UUID_A, [h.session_meta(), *exhausted_turn()])
        add(cli, uuid=UUID_A, name="alpha")
        scenario_exhausted(fake_binary[1])
        cli("tick")
        result = cli("check", "--no-deliver")
        assert result.exit_code == 11
        assert "not ready" in result.output

    def test_check_ready_exit_0(self, cli, fake_binary, home):
        """窗口滚动 + 新鲜实时证据 + ordinary 放行 → READY,退出码 0。"""

        write_rollout(home, UUID_A, [h.session_meta(), *exhausted_turn()])
        add(cli, uuid=UUID_A, name="alpha")
        scenario_exhausted(fake_binary[1])
        cli("tick")
        scenario_recovered(fake_binary[1])
        result = cli("--json", "check")
        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["decisions"] and payload["decisions"][0]["ready"] is True
        gates = {g["gate"]: g["status"] for g in payload["decisions"][0]["gates"]}
        assert gates["G6"] == "pass"

    def test_check_needs_human_exit_10(self, cli, fake_binary, home):
        write_rollout(home, UUID_A, [h.session_meta(), *exhausted_turn()])
        add(cli, uuid=UUID_A, name="alpha")
        path = (
            home / "sessions" / "2026" / "01" / "01" / f"rollout-2026-01-01T00-00-00-{UUID_A}.jsonl"
        )
        path.write_text(
            h.file_content([h.session_meta(), *h.failed_turn(start=60, end=120, turn_id="t-1")]),
            encoding="utf-8",
        )
        cli("tick")
        result = cli("check")
        assert result.exit_code == 10

    def test_check_no_deliver_zero_delivery_side_effects(self, cli, fake_binary, home):
        """check --no-deliver:零投递副作用(无 deliveries、无投递状态、只读方法)。"""

        write_rollout(home, UUID_A, [h.session_meta(), *exhausted_turn()])
        add(cli, uuid=UUID_A, name="alpha")
        scenario_exhausted(fake_binary[1])
        cli("tick")
        scenario_recovered(fake_binary[1])
        result = cli("--json", "check", "--no-deliver")
        assert result.exit_code in (0, 11)
        # 数据库断言:打开同一 state.db(经 --state-dir 不可得,从 XDG 读)
        from codex_autopilot.store import Store

        db_path = Path.home() / ".local" / "state" / "codex-autopilot" / "state.db"
        store = Store.open(db_path)
        try:
            assert store.query_one("SELECT COUNT(*) c FROM deliveries")["c"] == 0
            states = {r["state"] for r in store.query("SELECT state FROM session_state")}
            assert not states & {"DELIVERING", "DELIVERED_UNCONFIRMED"}
        finally:
            store.close()
        # Fake 日志:仅白名单只读方法(T-GUARD-06)
        _, _, log = fake_binary
        methods = set()
        for line in log.read_text().splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry["dir"] == "recv" and isinstance(entry["msg"], dict):
                methods.add(entry["msg"].get("method", ""))
        assert methods <= {"initialize", "initialized", "account/rateLimits/read"}
        assert not any("consume" in m for m in methods)
