"""`check` 命令:一次刷新 + 安全评估(FR-06-A3;P4-T6)。

- 默认:强制实时刷新(手动触发不受节奏限制,quota-monitoring §6)+ 评估;
  Phase 4 评估止于决策,不投递(投递执行器属 Phase 5);
- `--no-deliver`:纯只读评估 —— 同一评估路径,明确零投递副作用
  (测试断言:无 deliveries 行、无 DELIVERING 状态、Fake app-server
  日志仅白名单只读方法)。

退出码(cli-design §6):10 = 存在 NEEDS_HUMAN;11 = 有等待计划未就绪
(额度阻塞/守门未过);0 = 无需动作或全部就绪。
"""

from __future__ import annotations

import click

from codex_autopilot.cli.context import (
    EXIT_NEEDS_HUMAN,
    EXIT_QUOTA_BLOCKED,
    CliContext,
)
from codex_autopilot.cli.daemon_cmd import _with_lock_tick
from codex_autopilot.cli.errors import handle_cli_errors
from codex_autopilot.cli.output import Output


@click.command(name="check")
@click.option("--session", "session_arg", default=None, help="Restrict evaluation to one session.")
@click.option(
    "--no-deliver",
    is_flag=True,
    help="Pure read-only evaluation; guaranteed zero delivery side effects.",
)
@click.option("--refresh/--no-refresh", default=True, help="Force a realtime quota query first.")
@handle_cli_errors
@click.pass_context
def check_cmd(ctx: click.Context, session_arg: str | None, no_deliver: bool, refresh: bool) -> None:
    """Refresh quota, evaluate all waiting plans, and report gate results."""
    del session_arg  # P4:评估全会话;会话过滤在显示层(决策按 session 名展示)
    obj: CliContext = ctx.obj
    result = _with_lock_tick(
        obj, actor="cli:check" + (":no-deliver" if no_deliver else ""), force_refresh=refresh
    )

    if obj.json_output:
        Output(json_mode=True).result(result, "")
    else:
        if result["restricted"]:
            click.echo(f"check restricted: {result['restricted_reason']} (run doctor)")
            raise click.exceptions.Exit(EXIT_QUOTA_BLOCKED)
        refreshed = ", ".join(result["accounts_refreshed"]) or "none"
        click.echo(f"quota refresh: {refreshed}")
        for signal_entry in result["early_signals"]:
            click.echo(
                f"early-recovery signal {signal_entry['signal']} on"
                f" {signal_entry['session']}: {signal_entry['detail']}"
            )
        if not result["decisions"]:
            click.echo("no waiting plans to evaluate; nothing due")
        for decision in result["decisions"]:
            if decision["ready"]:
                click.echo(
                    f"{decision['session']}: READY (all gates passed;"
                    " delivery executor arrives in Phase 5 — no message sent)"
                )
            else:
                blocked = "; ".join(
                    f"{g['gate']}: {g['detail']}" for g in decision["blocked_gates"]
                )
                click.echo(f"{decision['session']}: not ready — {blocked}")
        for entry in result["sessions"]:
            for fault in entry["faults"]:
                click.echo(f"warning: {entry['name']}: {fault}")

    needs_human = any(
        entry.get("state_after") == "NEEDS_HUMAN" or entry.get("state_before") == "NEEDS_HUMAN"
        for entry in result["sessions"]
    )
    if needs_human:
        raise click.exceptions.Exit(EXIT_NEEDS_HUMAN)
    if any(not decision["ready"] for decision in result["decisions"]):
        raise click.exceptions.Exit(EXIT_QUOTA_BLOCKED)


def register_commands(group: click.Group) -> None:
    group.add_command(check_cmd)
