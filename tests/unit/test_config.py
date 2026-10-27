"""P2-T1 配置系统测试:合并优先级、XDG 路径、非法值拒绝(T-CLI-01 锚点)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codex_autopilot.config import ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(env, **kwargs):
    return load_config(env=env, **kwargs)


class TestDefaults:
    def test_builtin_defaults(self, isolated_home: Path):
        result = _load({})
        config = result.config
        assert config.state_dir == isolated_home / ".local" / "state" / "codex-autopilot"
        assert config.state_db.name == "state.db"
        assert config.lock_file.name == "daemon.lock"
        assert config.codex_home == isolated_home / ".codex"
        assert config.codex_binary == "codex"
        assert config.scheduler.tick_interval == 60
        assert config.scheduler.intensive_interval == 60
        assert config.quota.exhausted_threshold_pct == 99
        assert config.quota.resume_max_pct == 90
        assert config.quota.fresh_ttl_sec == 600
        assert config.quota.regular_probe_interval == 300
        assert config.quota.hint_min_pct == 90
        assert config.recovery.confirm_timeout_sec == 1800
        assert config.recovery.queue_timeout_sec == 45
        assert config.recovery.min_delivery_gap_sec == 600
        assert config.notify.console is True
        assert config.notify.safety_events_always_on is True
        assert config.notify.smtp.enabled is False

    def test_missing_default_config_file_is_silent(self, isolated_home: Path):
        result = _load({})
        assert result.config_file_used is None


class TestPriority:
    def test_file_overrides_defaults(self, tmp_path: Path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[scheduler]\ntick_interval = 10\n[quota]\nexhausted_threshold_pct = 95\n",
            encoding="utf-8",
        )
        config = _load({}, config_file=config_file).config
        assert config.scheduler.tick_interval == 10
        assert config.quota.exhausted_threshold_pct == 95
        assert config.quota.resume_max_pct == 90  # 未覆盖项保持默认

    def test_env_overrides_file(self, tmp_path: Path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[scheduler]\ntick_interval = 10\n", encoding="utf-8")
        config = _load(
            {"CODEX_AUTOPILOT_SCHEDULER__TICK_INTERVAL": "5"},
            config_file=config_file,
        ).config
        assert config.scheduler.tick_interval == 5

    def test_cli_overrides_env(self, tmp_path: Path):
        config = _load(
            {"CODEX_AUTOPILOT_PATHS__STATE_DIR": str(tmp_path / "from-env")},
            cli_overrides={"paths.state_dir": str(tmp_path / "from-cli")},
        ).config
        assert config.state_dir == tmp_path / "from-cli"

    def test_tilde_expansion_in_state_dir(self, isolated_home: Path, tmp_path: Path):
        config = _load(
            {"HOME": str(isolated_home)}, cli_overrides={"paths.state_dir": "~/x"}
        ).config
        assert config.state_dir == isolated_home / "x"

    def test_nested_env_section(self):
        config = _load({"CODEX_AUTOPILOT_NOTIFY__SMTP__HOST": "smtp.example.test"}).config
        assert config.notify.smtp.host == "smtp.example.test"


class TestEnvParsing:
    def test_bool_forms(self):
        config = _load({"CODEX_AUTOPILOT_NOTIFY__CONSOLE": "false"}).config
        assert config.notify.console is False
        config = _load({"CODEX_AUTOPILOT_NOTIFY__CONSOLE": "TRUE"}).config
        assert config.notify.console is True

    def test_bool_invalid(self):
        with pytest.raises(ConfigError, match="CODEX_AUTOPILOT_NOTIFY__CONSOLE"):
            _load({"CODEX_AUTOPILOT_NOTIFY__CONSOLE": "maybe"})

    def test_int_invalid(self):
        with pytest.raises(ConfigError):
            _load({"CODEX_AUTOPILOT_SCHEDULER__TICK_INTERVAL": "soon"})

    def test_list_env(self):
        config = _load(
            {"CODEX_AUTOPILOT_NOTIFY__SMTP__TO": "a@example.test, b@example.test"}
        ).config
        assert config.notify.smtp.to == ("a@example.test", "b@example.test")

    def test_empty_env_value_ignored(self):
        config = _load({"CODEX_AUTOPILOT_SCHEDULER__TICK_INTERVAL": ""}).config
        assert config.scheduler.tick_interval == 60


class TestValidation:
    def test_unknown_section_rejected(self, tmp_path: Path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[notasection]\nx = 1\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="notasection"):
            _load({}, config_file=config_file)

    def test_unknown_key_rejected(self, tmp_path: Path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[quota]\nexhausted_treshold_pct = 95\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="exhausted_treshold_pct"):
            _load({}, config_file=config_file)

    @pytest.mark.parametrize(
        "section, key, value",
        [
            ("quota", "exhausted_threshold_pct", 50),  # 越下界(95-100)
            ("quota", "exhausted_threshold_pct", 101),
            ("quota", "hint_min_pct", 99),  # 越上界(90-98)
            ("quota", "fresh_ttl_sec", 0),
            ("scheduler", "tick_interval", 0),
            ("recovery", "queue_timeout_sec", -1),
        ],
    )
    def test_range_violations(self, tmp_path: Path, section, key, value):
        config_file = tmp_path / "config.toml"
        config_file.write_text(f"[{section}]\n{key} = {value}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=rf"{section}\.{key}"):
            _load({}, config_file=config_file)

    def test_resume_threshold_must_be_below_exhaustion(self, tmp_path: Path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[quota]\nexhausted_threshold_pct = 95\nresume_max_pct = 95\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="resume_max_pct"):
            _load({}, config_file=config_file)

    def test_smtp_enabled_requires_fields(self, tmp_path: Path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[notify.smtp]\nenabled = true\nhost = "smtp.example.test"\n',
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match=r"notify\.smtp"):
            _load({}, config_file=config_file)

    def test_smtp_password_sources_exclusive(self, tmp_path: Path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[notify.smtp]\n"
            'enabled = true\nhost = "smtp.example.test"\nusername = "u"\n'
            'from = "a@example.test"\nto = ["b@example.test"]\n'
            'password_env = "X"\npassword_cmd = ["pass", "show", "x"]\n',
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="password_env / password_cmd"):
            _load({}, config_file=config_file)

    def test_smtp_enabled_valid_full_config(self, tmp_path: Path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[notify.smtp]\n"
            'enabled = true\nhost = "smtp.example.test"\nusername = "u"\n'
            'from = "a@example.test"\nto = ["b@example.test"]\n'
            'password_env = "SMTP_PASS"\n',
            encoding="utf-8",
        )
        config = _load({}, config_file=config_file).config
        assert config.notify.smtp.enabled is True

    def test_bad_toml_rejected(self, tmp_path: Path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[scheduler\ntick = 1\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="cannot parse"):
            _load({}, config_file=config_file)

    def test_explicit_config_missing_rejected(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="not found"):
            _load({}, config_file=tmp_path / "nope.toml")


class TestXdgPaths:
    def test_xdg_state_home_absolute(self, tmp_path: Path):
        config = _load({"XDG_STATE_HOME": str(tmp_path / "st")}).config
        assert config.state_dir == tmp_path / "st" / "codex-autopilot"

    def test_xdg_state_home_relative_rejected(self):
        with pytest.raises(ConfigError, match="XDG_STATE_HOME"):
            _load({"XDG_STATE_HOME": "relative/path"})

    def test_xdg_state_home_empty_ignored(self, isolated_home: Path):
        config = _load({"XDG_STATE_HOME": ""}).config
        assert config.state_dir == isolated_home / ".local" / "state" / "codex-autopilot"

    def test_relative_state_dir_rejected(self):
        with pytest.raises(ConfigError, match="absolute"):
            _load({}, cli_overrides={"paths.state_dir": "relative/dir"})


class TestExampleConfig:
    def test_examples_config_toml_loads(self):
        """T-CLI-01:示例配置与代码 schema 一致,且可解析。"""

        result = _load({}, config_file=REPO_ROOT / "examples" / "config.toml")
        config = result.config
        assert result.config_file_used is not None
        assert config.notify.smtp.enabled is False
        assert config.notify.smtp.host == "smtp.example.com"
        assert config.notify.smtp.retry_backoff_sec == (30, 120, 480)
