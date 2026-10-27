"""P3-T4 CLI 测试:`quota` 命令(T-CLI-QUOTA;S-08 降级、--json 契约)。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codex_autopilot.store import Store
from tests.conftest import write_scenario

UUID_A = "01234567-89ab-cdef-0123-456789abcdef"
UUID_B = "fedcba98-7654-3210-fedc-ba9876543210"

WINDOW_JSON_KEYS = {
    "account",
    "source",
    "captured_at",
    "stale",
    "window_kind",
    "inferred",
    "used_percent",
    "resets_at",
    "window_minutes",
    "exhausted",
}


@pytest.fixture
def fake_binary(monkeypatch, fake_appserver_env):
    """把 codex.binary 指向 Fake app-server 脚本(config env 覆盖,P2 契约)。"""

    binary, scenario, log = fake_appserver_env
    monkeypatch.setenv("CODEX_AUTOPILOT_CODEX__BINARY", str(binary))
    return binary, scenario, log


@pytest.fixture
def cli_store(fake_clock):
    """打开 CLI 所用的 state.db(同一 XDG 隔离环境)。"""

    db = Path(os.environ["XDG_STATE_HOME"]) / "codex-autopilot" / "state.db"
    store = Store.open(db, clock=fake_clock)
    yield store
    store.close()


def register(cli, codex_home, *extra, uuid=UUID_A, name="alpha"):
    result = cli(
        "--json",
        "add",
        "--session",
        uuid,
        "--name",
        name,
        "--codex-home",
        str(codex_home),
        "--project",
        "/tmp",
        *extra,
    )
    assert result.exit_code == 0, result.output
    return result


class TestQuotaBasics:
    def test_empty_db_human_output(self, cli):
        result = cli("quota")
        assert result.exit_code == 0
        assert "no quota data yet" in result.output

    def test_empty_db_json_is_empty_array(self, cli):
        result = cli("--json", "quota")
        assert result.exit_code == 0
        assert json.loads(result.stdout) == []

    def test_json_schema_locked(self, cli, fake_binary, codex_home):
        """--json 每窗口字段集与 cli-design §3 固化 schema 一致。"""

        _, scenario, _ = fake_binary
        write_scenario(scenario)
        register(cli, codex_home)
        result = cli("--json", "quota", "--refresh")
        assert result.exit_code == 0, result.output
        rows = json.loads(result.stdout)
        assert rows, "expected window rows"
        for row in rows:
            assert set(row) == WINDOW_JSON_KEYS
        kinds = {row["window_kind"] for row in rows}
        assert kinds == {"five_hour", "weekly"}
        five = next(row for row in rows if row["window_kind"] == "five_hour")
        assert five["source"] == "appserver"
        assert five["used_percent"] == 41
        assert five["window_minutes"] == 300
        assert five["exhausted"] is False
        assert five["inferred"] is False
        assert five["stale"] is False
        assert five["account"]["codex_home"] == str(codex_home.resolve())

    def test_human_output_format(self, cli, fake_binary, codex_home):
        _, scenario, _ = fake_binary
        write_scenario(scenario)
        register(cli, codex_home)
        result = cli("quota", "--refresh")
        assert result.exit_code == 0
        assert "appserver (realtime)" in result.output
        assert "five_hour" in result.output
        assert "weekly" in result.output
        assert "41%" in result.output
        assert "resets:" in result.output
        assert "window_minutes=300" in result.output

    def test_exhausted_marker_shown(self, cli, fake_binary, codex_home):
        _, scenario, _ = fake_binary
        write_scenario(
            scenario,
            rate_limits_result={
                "rateLimitsByLimitId": {
                    "codex": {
                        "primary": {
                            "usedPercent": 100,
                            "resetsAt": 1792583368,
                            "windowDurationMins": 300,
                        },
                        "secondary": {
                            "usedPercent": 41,
                            "resetsAt": 1793099965,
                            "windowDurationMins": 10080,
                        },
                    }
                },
                "ordinaryUsageAllowed": False,
            },
        )
        register(cli, codex_home)
        result = cli("--json", "quota", "--refresh")
        rows = json.loads(result.stdout)
        five = next(row for row in rows if row["window_kind"] == "five_hour")
        assert five["exhausted"] is True
        human = cli("quota")
        assert "EXHAUSTED" in human.output  # 库内快照(刚刷新过)


class TestSources:
    def test_auto_falls_back_to_jsonl_history(self, cli, codex_home, write_rollout, fixture_path):
        """S-08/T-QUOTA-07:无实时数据时降级展示 JSONL 历史,标非实时。"""

        register(cli, codex_home)
        write_rollout(
            codex_home, UUID_A, fixture_path("jsonl", "token_count_normal.jsonl").read_text()
        )
        result = cli("--json", "quota")
        assert result.exit_code == 0
        rows = json.loads(result.stdout)
        assert rows and {row["source"] for row in rows} == {"jsonl"}
        human = cli("quota")
        assert "jsonl (historical)" in human.output  # 非实时标注(FR-04-A3)

    def test_source_jsonl_explicit(self, cli, codex_home, write_rollout, fixture_path):
        register(cli, codex_home)
        write_rollout(
            codex_home, UUID_A, fixture_path("jsonl", "token_count_normal.jsonl").read_text()
        )
        result = cli("--json", "quota", "--source", "jsonl")
        rows = json.loads(result.stdout)
        assert rows and rows[0]["used_percent"] == 47.0

    def test_source_appserver_without_data_reports_empty(self, cli, codex_home):
        register(cli, codex_home)
        result = cli("quota", "--source", "appserver")
        assert result.exit_code == 0
        assert "no quota data yet" in result.output
        assert json.loads(cli("--json", "quota", "--source", "appserver").stdout) == []

    def test_refresh_failure_falls_back_marked_non_realtime(
        self, cli, fake_binary, codex_home, write_rollout, fixture_path
    ):
        """实时查询失败 → 警告 + 展示历史(jsonl)数据,不伪装实时。"""

        _, scenario, _ = fake_binary
        register(cli, codex_home)
        write_rollout(
            codex_home, UUID_A, fixture_path("jsonl", "token_count_normal.jsonl").read_text()
        )
        write_scenario(scenario, hang_rate_limits=True)
        result = cli("--json", "quota", "--refresh")
        assert result.exit_code == 0  # 信息性降级,不是错误
        assert "realtime query failed" in result.output  # 警告(stderr 混入 output)
        rows = json.loads(result.stdout)
        assert {row["source"] for row in rows} == {"jsonl"}

    def test_refresh_beats_stored_history(
        self, cli, fake_binary, codex_home, write_rollout, fixture_path
    ):
        """实时成功后视图切到 appserver(captured_at 更新)。"""

        _, scenario, _ = fake_binary
        register(cli, codex_home)
        write_rollout(
            codex_home, UUID_A, fixture_path("jsonl", "token_count_normal.jsonl").read_text()
        )
        cli("quota")  # 先落 jsonl 历史
        write_scenario(scenario)
        rows = json.loads(cli("--json", "quota", "--refresh").stdout)
        assert {row["source"] for row in rows} == {"appserver"}


class TestAccountSelection:
    def test_account_not_found(self, cli):
        result = cli("--json", "quota", "--account", "missing")
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"]["code"] == "account_not_found"

    def test_account_by_codex_home(self, cli, fake_binary, codex_home):
        _, scenario, _ = fake_binary
        write_scenario(scenario)
        register(cli, codex_home)
        result = cli("--json", "quota", "--account", str(codex_home.resolve()), "--refresh")
        assert result.exit_code == 0, result.output
        rows = json.loads(result.stdout)
        assert rows and rows[0]["account"]["codex_home"] == str(codex_home.resolve())

    def test_accounts_shown_separately(self, cli, tmp_path, write_rollout, fixture_path):
        """S-09/FR-02-A6:多账户并列展示,绝不混合。"""

        home1 = tmp_path / "h1"
        home2 = tmp_path / "h2"
        home1.mkdir()
        home2.mkdir()
        register(cli, home1, uuid=UUID_A, name="alpha")
        register(cli, home2, uuid=UUID_B, name="beta")
        write_rollout(home1, UUID_A, fixture_path("jsonl", "token_count_normal.jsonl").read_text())
        write_rollout(home2, UUID_B, fixture_path("jsonl", "token_count_5h_100.jsonl").read_text())
        result = cli("--json", "quota")
        rows = json.loads(result.stdout)
        homes = {row["account"]["codex_home"] for row in rows}
        assert homes == {str(home1.resolve()), str(home2.resolve())}
        human = cli("quota")
        assert human.output.count("account:") == 2
        assert "jsonl (historical)" in human.output

    def test_account_label_resolution(self, cli, codex_home, cli_store):
        """--account 按 label 精确解析。"""

        register(cli, codex_home)
        cli_store.execute(
            "UPDATE accounts SET label = 'main' WHERE codex_home = ?", (str(codex_home.resolve()),)
        )
        result = cli("--json", "quota", "--account", "main")
        assert result.exit_code == 0, result.output


class TestRefreshSideEffects:
    def test_refresh_persists_snapshot(self, cli, fake_binary, codex_home, cli_store):
        _, scenario, _ = fake_binary
        write_scenario(scenario)
        register(cli, codex_home)
        cli("--json", "quota", "--refresh")
        row = cli_store.query_one("SELECT * FROM quota_snapshots WHERE source = 'appserver'")
        assert row is not None and row["ok"] == 1

    def test_no_queue_or_delivery_side_effects(self, cli, fake_binary, codex_home, cli_store):
        """红线:quota 只读监控——不产生 failures/deliveries/plans 行。"""

        _, scenario, _ = fake_binary
        write_scenario(scenario)
        register(cli, codex_home)
        cli("quota", "--refresh")
        for table in ("failures", "deliveries", "recovery_plans"):
            assert cli_store.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0

    def test_codex_files_never_written(self, cli, codex_home, write_rollout, fixture_path):
        register(cli, codex_home)
        rollout = write_rollout(
            codex_home, UUID_A, fixture_path("jsonl", "token_count_normal.jsonl").read_text()
        )
        before = rollout.read_bytes()
        cli("quota")
        cli("--json", "quota")
        assert rollout.read_bytes() == before  # 不修改 Codex 原始文件(红线)


class TestUsageGuard:
    def test_refresh_with_jsonl_source_warns(self, cli, fake_binary, codex_home):
        _, scenario, _ = fake_binary
        write_scenario(scenario)
        register(cli, codex_home)
        result = cli("quota", "--refresh", "--source", "jsonl")
        assert result.exit_code == 0
        assert "no effect with --source jsonl" in result.output

    def test_removed_session_account_still_queryable(self, cli, fake_binary, codex_home):
        """软删除全部会话后:账户仍可查询(只读回退),并有提示。"""

        _, scenario, _ = fake_binary
        write_scenario(scenario)
        register(cli, codex_home)
        cli("remove", "alpha")  # 软删除 → 账户仍在但无监控会话
        result = cli("quota")
        assert result.exit_code == 0
        assert "querying all known accounts" in result.output
        assert result.output.count("account:") == 1
