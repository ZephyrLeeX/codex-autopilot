"""XDG 目录解析与路径规范化(deployment.md §2,D11:零硬编码)。

规则遵循 XDG Base Directory Specification:
- `$XDG_CONFIG_HOME` / `$XDG_STATE_HOME` 仅在**非空且为绝对路径**时生效;
- 缺省分别回退 `~/.config`、`~/.local/state`;
- 应用子目录固定为 `codex-autopilot`(包名,非用户数据)。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

APP_DIR_NAME = "codex-autopilot"
CONFIG_FILE_NAME = "config.toml"
STATE_DB_NAME = "state.db"
LOCK_FILE_NAME = "daemon.lock"


class XdgPathError(ValueError):
    """XDG 环境变量取值非法(非绝对路径)。"""


def _xdg_base(env_var: str, default_tail: str, env: Mapping[str, str]) -> Path:
    value = env.get(env_var, "")
    if value:
        path = Path(value)
        if not path.is_absolute():
            raise XdgPathError(
                f"${env_var} must be an absolute path, got {value!r} (XDG Base Directory spec)"
            )
        return path
    return Path.home() / default_tail


def default_config_dir(env: Mapping[str, str] | None = None) -> Path:
    env_map = os.environ if env is None else env
    return _xdg_base("XDG_CONFIG_HOME", ".config", env_map) / APP_DIR_NAME


def default_config_file(env: Mapping[str, str] | None = None) -> Path:
    return default_config_dir(env) / CONFIG_FILE_NAME


def default_state_dir(env: Mapping[str, str] | None = None) -> Path:
    env_map = os.environ if env is None else env
    return _xdg_base("XDG_STATE_HOME", ".local/state", env_map) / APP_DIR_NAME


def state_db_path(state_dir: Path) -> Path:
    return state_dir / STATE_DB_NAME


def lock_file_path(state_dir: Path) -> Path:
    return state_dir / LOCK_FILE_NAME


def expand_path(value: str | Path) -> Path:
    """展开 `~` 并转为绝对路径(不解析符号链接,用于配置路径)。

    相对路径一律拒绝:守护进程的 cwd 不确定,cwd 相对路径是隐患。
    """

    path = Path(os.path.expanduser(str(value)))
    if not path.is_absolute():
        raise XdgPathError(
            f"path {value!r} must be absolute (or start with ~); relative paths are rejected"
        )
    return path
