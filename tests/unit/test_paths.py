"""P2-T1 XDG 路径测试(deployment.md §2、D11)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codex_autopilot.util.paths import (
    default_config_file,
    default_state_dir,
    lock_file_path,
    state_db_path,
)


class TestXdgDefaults:
    def test_defaults_under_home(self, isolated_home: Path):
        env = {"HOME": str(isolated_home)}
        assert (
            default_config_file(env)
            == isolated_home / ".config" / "codex-autopilot" / "config.toml"
        )
        assert default_state_dir(env) == isolated_home / ".local" / "state" / "codex-autopilot"

    def test_overrides_respected(self, tmp_path: Path):
        env = {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_STATE_HOME": str(tmp_path / "st"),
        }
        assert default_config_file(env) == tmp_path / "cfg" / "codex-autopilot" / "config.toml"
        assert default_state_dir(env) == tmp_path / "st" / "codex-autopilot"

    def test_empty_env_falls_back_to_home(self, isolated_home: Path):
        env = {"XDG_CONFIG_HOME": "", "XDG_STATE_HOME": "", "HOME": str(isolated_home)}
        assert (
            default_config_file(env)
            == isolated_home / ".config" / "codex-autopilot" / "config.toml"
        )

    def test_relative_env_rejected(self):
        from codex_autopilot.util.paths import XdgPathError

        with pytest.raises(XdgPathError):
            default_state_dir({"XDG_STATE_HOME": "rel"})


def test_derived_paths(tmp_path: Path):
    assert state_db_path(tmp_path).name == "state.db"
    assert lock_file_path(tmp_path).name == "daemon.lock"
