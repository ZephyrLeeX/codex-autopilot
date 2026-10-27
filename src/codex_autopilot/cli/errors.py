"""命令级错误处理:领域/基础设施异常 → 稳定退出码 + --json 错误体(cli-design §6)。"""

from __future__ import annotations

import functools
import sqlite3

import click

from codex_autopilot.cli.context import (
    EXIT_CONFIG_DB,
    EXIT_LOCK_HELD,
    CliContext,
    CliError,
)
from codex_autopilot.cli.output import emit_error
from codex_autopilot.config import ConfigError
from codex_autopilot.scheduling.single_instance import LockBusyError
from codex_autopilot.store.errors import StoreError


def _error_payload_and_exit(json_mode: bool, code: str, message: str, exit_code: int):
    emit_error(json_mode=json_mode, code=code, message=message)
    raise click.exceptions.Exit(exit_code)


def handle_cli_errors(func):
    """装饰命令回调:统一异常→退出码映射;结束时关闭 Store 连接。"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        ctx = click.get_current_context(silent=True)
        json_mode = bool(ctx and ctx.obj and ctx.obj.json_output)
        try:
            return func(*args, **kwargs)
        except (click.exceptions.Exit, click.UsageError):
            raise
        except CliError as exc:
            _error_payload_and_exit(json_mode, exc.code, str(exc), exc.exit_code)
        except LockBusyError as exc:
            _error_payload_and_exit(json_mode, "lock_held", str(exc), EXIT_LOCK_HELD)
        except (ConfigError, StoreError) as exc:
            _error_payload_and_exit(json_mode, "config_or_database", str(exc), EXIT_CONFIG_DB)
        except sqlite3.Error as exc:
            _error_payload_and_exit(
                json_mode, "config_or_database", f"database error: {exc}", EXIT_CONFIG_DB
            )
        finally:
            if ctx and ctx.obj and isinstance(ctx.obj, CliContext):
                ctx.obj.close()

    return wrapper
