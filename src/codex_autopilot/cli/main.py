"""CLI 入口:全局选项、配置装配(P2-T4)。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import click

from codex_autopilot import __version__
from codex_autopilot.cli.context import EXIT_CONFIG_DB, make_context
from codex_autopilot.cli.output import emit_error
from codex_autopilot.cli.sessions import register_commands
from codex_autopilot.config import ConfigError, load_config
from codex_autopilot.util.jsonlog import setup_logging


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="codex-autopilot")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=False, dir_okay=False, path_type=Path),
    default=None,
    help="Path to config.toml (default: $XDG_CONFIG_HOME/codex-autopilot/config.toml).",
)
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the state directory (priority: CLI > env > config > XDG default).",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Machine-readable single-line JSON on stdout.",
)
@click.option("--verbose", is_flag=True, help="Debug logging.")
@click.option("--quiet", is_flag=True, help="Warnings and errors only.")
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable ANSI colors (informational; output is plain by default).",
)
@click.pass_context
def main(
    ctx: click.Context,
    config_path: Path | None,
    state_dir: Path | None,
    json_output: bool,
    verbose: bool,
    quiet: bool,
    no_color: bool,
) -> None:
    """codex-autopilot -- Codex CLI session monitor with safe auto-resume."""
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    setup_logging(level)
    cli_overrides = {"paths.state_dir": str(state_dir)} if state_dir is not None else None
    try:
        result = load_config(env=os.environ, config_file=config_path, cli_overrides=cli_overrides)
    except ConfigError as exc:
        emit_error(json_mode=json_output, code="config", message=str(exc))
        raise click.exceptions.Exit(EXIT_CONFIG_DB) from None
    ctx.obj = make_context(config=result.config, json_output=json_output)
    del no_color  # 输出本就无 ANSI 颜色;接受该开关以保持契约稳定


register_commands(main)


if __name__ == "__main__":
    main()
