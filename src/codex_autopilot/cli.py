"""CLI 骨架。

Phase 1 交付物:仅保证 `codex-autopilot --version` 可用与打包入口有效。
全部子命令在 Phase 2+ 按 docs/cli-design.md 实现。
"""

from __future__ import annotations

import click

from codex_autopilot import __version__


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="codex-autopilot")
def main() -> None:
    """codex-autopilot —— Codex CLI 会话监控与安全自动恢复(实现待 Phase 2+)。"""


if __name__ == "__main__":
    main()
