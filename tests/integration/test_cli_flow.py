"""P2 集成冒烟:真实 CLI 全链路(add → list → remove)、路径落点、state-dir 优先级。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from codex_autopilot.store import Store

UUID_A = "01234567-89ab-cdef-0123-456789abcdef"
UUID_B = "fedcba98-7654-3210-fedc-ba9876543210"


class TestFullFlow:
    def test_add_list_remove_roundtrip(self, cli, codex_home, tmp_path):
        assert cli("--json", "list").exit_code == 0
        first = cli(
            "--json",
            "add",
            "--session",
            UUID_A,
            "--name",
            "alpha",
            "--codex-home",
            str(codex_home),
            "--project",
            str(tmp_path),
        )
        second = cli(
            "--json",
            "add",
            "--session",
            UUID_B,
            "--name",
            "beta",
            "--codex-home",
            str(codex_home),
            "--project",
            str(tmp_path),
        )
        assert first.exit_code == 0 and second.exit_code == 0
        items = json.loads(cli("--json", "list").stdout)
        assert [item["name"] for item in items] == ["alpha", "beta"]
        assert items[0]["account"]["codex_home"] == str(codex_home.resolve())  # 同账户

        assert cli("--json", "remove", "alpha").exit_code == 0
        items = json.loads(cli("--json", "list").stdout)
        assert [item["name"] for item in items] == ["beta"]

    def test_db_created_at_xdg_state_home(self, cli, codex_home, tmp_path):
        assert cli("--json", "list").exit_code == 0
        db = Path(os.environ["XDG_STATE_HOME"]) / "codex-autopilot" / "state.db"
        assert db.is_file()
        with Store.open(db) as store:
            assert store.query_one("PRAGMA journal_mode")["journal_mode"].lower() == "wal"


class TestStateDirPriority:
    def test_config_file_beats_xdg_default(self, cli, codex_home, tmp_path):
        config_dir = Path(os.environ["XDG_CONFIG_HOME"]) / "codex-autopilot"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text(
            f'[paths]\nstate_dir = "{tmp_path / "from-file"}"\n', encoding="utf-8"
        )
        assert cli("--json", "list").exit_code == 0
        assert (tmp_path / "from-file" / "state.db").is_file()

    def test_env_beats_config_file(self, cli, codex_home, tmp_path, monkeypatch):
        config_dir = Path(os.environ["XDG_CONFIG_HOME"]) / "codex-autopilot"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text(
            f'[paths]\nstate_dir = "{tmp_path / "from-file"}"\n', encoding="utf-8"
        )
        monkeypatch.setenv("CODEX_AUTOPILOT_PATHS__STATE_DIR", str(tmp_path / "from-env"))
        assert cli("--json", "list").exit_code == 0
        assert (tmp_path / "from-env" / "state.db").is_file()
        assert not (tmp_path / "from-file" / "state.db").exists()

    def test_cli_flag_beats_env(self, cli, codex_home, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_AUTOPILOT_PATHS__STATE_DIR", str(tmp_path / "from-env"))
        assert cli("--json", "--state-dir", str(tmp_path / "from-cli"), "list").exit_code == 0
        assert (tmp_path / "from-cli" / "state.db").is_file()
        assert not (tmp_path / "from-env" / "state.db").exists()


class TestErrorContract:
    def test_json_error_single_line(self, cli, codex_home, tmp_path):
        result = cli("--json", "add", "--session", "junk", "--name", "x")
        assert result.exit_code == 2
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert set(payload) == {"error"}
        assert set(payload["error"]) == {"code", "message"}

    def test_bad_config_file_exit_20(self, cli, tmp_path):
        bad = tmp_path / "bad.toml"
        bad.write_text("[scheduler]\ntick_interval = 0\n", encoding="utf-8")
        result = cli("--json", "--config", str(bad), "list")
        assert result.exit_code == 20
        assert json.loads(result.stdout)["error"]["code"] == "config"

    def test_missing_explicit_config_exit_20(self, cli, tmp_path):
        result = cli("--json", "--config", str(tmp_path / "nope.toml"), "list")
        assert result.exit_code == 20
