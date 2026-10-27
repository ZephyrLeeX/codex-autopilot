"""守护进程与生命周期命令:tick / run / pause / resume(P4-T5)。

- `tick`:单次调度周期,短暂持单实例锁(失败退出码 21,S-17);
- `run [--foreground]`:常驻循环(锁 + SIGTERM/SIGINT 优雅退出);
- `pause [--session]` / `resume [--session] [--ack-needs-human]`:
  三层开关中的全局暂停与会话 enabled(session-management §4)。
"""

from __future__ import annotations

import signal
import threading

import click

from codex_autopilot.cli.context import EXIT_ERROR, EXIT_USAGE, CliContext, CliError
from codex_autopilot.cli.errors import handle_cli_errors
from codex_autopilot.cli.output import Output
from codex_autopilot.scheduling.scheduler import Scheduler
from codex_autopilot.scheduling.single_instance import SingleInstanceLock
from codex_autopilot.store.dao import RuntimeFlagsDao, SessionDao
from codex_autopilot.util.audit import write_audit


def _make_scheduler(obj: CliContext) -> Scheduler:
    return Scheduler(obj.store(), obj.config, obj.clock)


def _with_lock_tick(obj: CliContext, *, actor: str, force_refresh: bool) -> dict:
    """短暂持锁执行一次 tick;锁被占由异常处理映射退出码 21。"""

    lock = SingleInstanceLock(obj.config.lock_file)
    lock.acquire()
    try:
        scheduler = _make_scheduler(obj)
        try:
            result = scheduler.tick(actor=actor, force_quota_refresh=force_refresh)
        finally:
            scheduler.close()
        return result.to_json()
    finally:
        lock.release()


@click.command(name="tick")
@handle_cli_errors
@click.pass_context
def tick_cmd(ctx: click.Context) -> None:
    """Run one scheduling cycle (equivalent to one daemon tick; timer mode)."""
    obj: CliContext = ctx.obj
    result = _with_lock_tick(obj, actor="cli:tick", force_refresh=False)
    if obj.json_output:
        Output(json_mode=True).result(result, "")
        return
    if result["restricted"]:
        click.echo(f"tick restricted: {result['restricted_reason']} (delivery frozen; run doctor)")
        return
    for entry in result["sessions"]:
        actions = ", ".join(entry["actions"]) or "-"
        faults = f"  faults: {'; '.join(entry['faults'])}" if entry["faults"] else ""
        click.echo(
            f"{entry['name']}: {entry['state_before'] or '-'} ->"
            f" {entry['state_after'] or '-'}  [{actions}]{faults}"
        )
    for decision in result["decisions"]:
        blocked = ", ".join(f"{g['gate']}({g['detail']})" for g in decision["blocked_gates"])
        if decision["ready"]:
            status = "READY (delivery executor arrives in Phase 5)"
        else:
            status = f"not ready - {blocked}"
        click.echo(f"decision for {decision['session']}: {status}")
    if not result["sessions"]:
        click.echo("no registered sessions; nothing to do")


@click.command(name="run")
@click.option("--foreground", is_flag=True, help="Accepted for compatibility; run is foreground.")
@handle_cli_errors
@click.pass_context
def run_cmd(ctx: click.Context, foreground: bool) -> None:
    """Run the scheduler daemon loop (single instance; SIGTERM for graceful exit)."""
    del foreground  # 守护进程本就前台运行(systemd 托管后台化)
    obj: CliContext = ctx.obj
    lock = SingleInstanceLock(obj.config.lock_file)
    lock.acquire()
    stop = threading.Event()

    def _stop(signum, frame):
        del signum, frame
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    interval = obj.config.scheduler.tick_interval
    scheduler = _make_scheduler(obj)
    try:
        click.echo(f"scheduler running (tick every {interval}s); press Ctrl-C to stop")
        while not stop.is_set():
            result = scheduler.tick(actor="daemon")
            if result.restricted:
                click.echo(
                    f"restricted mode: {result.restricted_reason};"
                    " scheduling frozen (doctor advised); retrying next tick"
                )
            stop.wait(interval)
    finally:
        scheduler.close()
        lock.release()
        click.echo("scheduler stopped gracefully")


# -- pause / resume ---------------------------------------------------------


@click.command(name="pause")
@click.option("--session", "session_arg", default=None, help="Pause one session (default: global).")
@handle_cli_errors
@click.pass_context
def pause_cmd(ctx: click.Context, session_arg: str | None) -> None:
    """Pause scheduling: one session (enabled=0) or globally (runtime flag)."""
    obj: CliContext = ctx.obj
    store = obj.store()
    now = obj.clock.epoch()
    if session_arg is None:
        with store.transaction() as conn:
            RuntimeFlagsDao(store).set("global_pause", True, now=now)
            write_audit(
                conn,
                ts=now,
                actor="cli:pause",
                category="admin",
                level="info",
                summary="global pause enabled (delivery frozen; monitoring continues)",
            )
        Output(json_mode=obj.json_output).result({"global_pause": True}, "global pause enabled")
        return
    row = SessionDao(store).resolve(session_arg)
    if row is None:
        raise CliError(f"session not found: {session_arg}", exit_code=EXIT_ERROR, code="not_found")
    with store.transaction() as conn:
        conn.execute(
            "UPDATE sessions SET enabled = 0, updated_at = ? WHERE uuid = ?", (now, row["uuid"])
        )
        write_audit(
            conn,
            ts=now,
            actor="cli:pause",
            category="admin",
            level="info",
            summary=f"session {row['name']} paused (all automatic transitions frozen)",
            session_uuid=row["uuid"],
        )
    Output(json_mode=obj.json_output).result(
        {"name": row["name"], "uuid": row["uuid"], "enabled": False},
        f"paused session '{row['name']}'",
    )


@click.command(name="resume")
@click.option(
    "--session", "session_arg", default=None, help="Resume one session (default: global)."
)
@click.option(
    "--ack-needs-human",
    is_flag=True,
    help="Required to leave NEEDS_HUMAN back to ACTIVE (explicit acknowledgement).",
)
@handle_cli_errors
@click.pass_context
def resume_cmd(ctx: click.Context, session_arg: str | None, ack_needs_human: bool) -> None:
    """Resume scheduling: one session or globally; NEEDS_HUMAN needs explicit ack."""
    obj: CliContext = ctx.obj
    store = obj.store()
    now = obj.clock.epoch()
    if session_arg is None:
        with store.transaction() as conn:
            RuntimeFlagsDao(store).set("global_pause", False, now=now)
            write_audit(
                conn,
                ts=now,
                actor="cli:resume",
                category="admin",
                level="info",
                summary="global pause cleared",
            )
        Output(json_mode=obj.json_output).result({"global_pause": False}, "global pause cleared")
        return

    row = SessionDao(store).resolve(session_arg)
    if row is None:
        raise CliError(f"session not found: {session_arg}", exit_code=EXIT_ERROR, code="not_found")
    state_row = store.query_one(
        "SELECT * FROM session_state WHERE session_uuid = ?", (row["uuid"],)
    )
    if state_row is not None and state_row["state"] == "NEEDS_HUMAN" and not ack_needs_human:
        raise CliError(
            f"session '{row['name']}' is in NEEDS_HUMAN; pass --ack-needs-human to confirm"
            " human review before resuming",
            exit_code=EXIT_USAGE,
            code="ack_required",
        )
    acked = False
    with store.transaction() as conn:
        conn.execute(
            "UPDATE sessions SET enabled = 1, updated_at = ? WHERE uuid = ?", (now, row["uuid"])
        )
        write_audit(
            conn,
            ts=now,
            actor="cli:resume",
            category="admin",
            level="info",
            summary=f"session {row['name']} resumed (enabled=1)",
            session_uuid=row["uuid"],
        )
    if state_row is not None and state_row["state"] == "NEEDS_HUMAN" and ack_needs_human:
        from codex_autopilot.recovery.state_machine import RecoveryStateMachine

        RecoveryStateMachine(store, obj.clock).ack_needs_human(row["uuid"], operator="cli:resume")
        acked = True
    Output(json_mode=obj.json_output).result(
        {
            "name": row["name"],
            "uuid": row["uuid"],
            "enabled": True,
            "ack_needs_human": acked,
        },
        f"resumed session '{row['name']}'" + (" (needs_human acknowledged)" if acked else ""),
    )


def register_commands(group: click.Group) -> None:
    group.add_command(tick_cmd)
    group.add_command(run_cmd)
    group.add_command(pause_cmd)
    group.add_command(resume_cmd)
