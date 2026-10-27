"""`history` 命令:恢复与投递历史(cli-design.md §3;P4-T5)。

按时间倒序合并四类事件:失败记录、恢复计划、状态迁移、投递(Phase 4
恒为空,Phase 5 起有数据)。--since 接受 Unix 秒或 ISO 日期(YYYY-MM-DD
[HH:MM:SS],本地时区)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import click

from codex_autopilot.cli.context import EXIT_ERROR, EXIT_USAGE, CliContext, CliError
from codex_autopilot.cli.errors import handle_cli_errors
from codex_autopilot.cli.output import Output, format_ts
from codex_autopilot.store.dao import SessionDao
from codex_autopilot.store.recovery_dao import SessionStateDao


def _parse_since(raw: str) -> int | None:
    text = raw.strip()
    if text.isdigit():
        return int(text)
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, pattern)
            return int(parsed.timestamp())
        except ValueError:
            continue
    return None


def _collect(
    store, session_uuid: str | None, since: int | None, limit: int
) -> list[dict[str, Any]]:
    """合并 failures / plans / state_history / deliveries,按时间倒序。"""

    events: list[dict[str, Any]] = []
    params: list[Any] = []
    where_session = " AND f.session_uuid = ?" if session_uuid else ""
    if session_uuid:
        params.append(session_uuid)
    failures = store.query(
        "SELECT f.*, s.name AS session_name FROM failures f"
        f" JOIN sessions s ON s.uuid = f.session_uuid WHERE 1=1{where_session}"
        " ORDER BY f.detected_at DESC LIMIT 500",
        tuple(params),
    )
    for row in failures:
        events.append(
            {
                "type": "failure",
                "ts": int(row["detected_at"]),
                "session": row["session_name"],
                "session_uuid": row["session_uuid"],
                "id": int(row["id"]),
                "kind": row["kind"],
                "turn_id": row["turn_id"],
                "status": row["status"],
                "closed_at": row["closed_at"],
            }
        )
        plan = store.query_one("SELECT * FROM recovery_plans WHERE failure_id = ?", (row["id"],))
        if plan is not None:
            events.append(
                {
                    "type": "plan",
                    "ts": int(plan["created_at"]),
                    "session": row["session_name"],
                    "session_uuid": row["session_uuid"],
                    "id": int(plan["id"]),
                    "failure_id": int(plan["failure_id"]),
                    "strategy": plan["strategy"],
                    "not_before": int(plan["not_before"]),
                    "status": plan["status"],
                }
            )
    deliveries = store.query(
        "SELECT d.*, f.session_uuid, s.name AS session_name FROM deliveries d"
        " JOIN failures f ON f.id = d.failure_id"
        f" JOIN sessions s ON s.uuid = f.session_uuid WHERE 1=1{where_session}"
        " ORDER BY d.started_at DESC LIMIT 200",
        tuple(params),
    )
    for row in deliveries:
        events.append(
            {
                "type": "delivery",
                "ts": int(row["started_at"]),
                "session": row["session_name"],
                "session_uuid": row["session_uuid"],
                "id": int(row["id"]),
                "failure_id": int(row["failure_id"]),
                "attempt_no": int(row["attempt_no"]),
                "outcome": row["outcome"],
                "ended_at": row["ended_at"],
            }
        )
    for row in SessionStateDao(store).history(session_uuid, limit=500):
        events.append(
            {
                "type": "transition",
                "ts": int(row["ts"]),
                "session_uuid": row["session_uuid"],
                "from_state": row["from_state"],
                "to_state": row["to_state"],
                "reason": row["reason"],
            }
        )
    name_by_uuid = {row["uuid"]: row["name"] for row in SessionDao(store).list_active()}
    for event in events:
        if "session" not in event:
            event["session"] = name_by_uuid.get(event.get("session_uuid"))
    if since is not None:
        events = [event for event in events if event["ts"] >= since]
    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[:limit]


@click.command(name="history")
@click.option("--session", "session_arg", default=None, help="Filter by session name or UUID.")
@click.option(
    "--since", default=None, help="Only events at/after this time (Unix sec or ISO date)."
)
@click.option("--limit", type=int, default=50, show_default=True, help="Max events to show.")
@handle_cli_errors
@click.pass_context
def history_cmd(ctx: click.Context, session_arg: str | None, since: str | None, limit: int) -> None:
    """Show recovery/delivery history (newest first)."""
    obj: CliContext = ctx.obj
    store = obj.store()
    session_uuid: str | None = None
    if session_arg is not None:
        row = SessionDao(store).resolve(session_arg)
        if row is None:
            raise CliError(
                f"session not found: {session_arg}", exit_code=EXIT_ERROR, code="not_found"
            )
        session_uuid = row["uuid"]
    since_epoch = _parse_since(since) if since is not None else None
    if since is not None and since_epoch is None:
        raise CliError(
            f"cannot parse --since {since!r} (use Unix seconds or YYYY-MM-DD[ HH:MM:SS])",
            exit_code=EXIT_USAGE,
            code="invalid_since",
        )
    events = _collect(store, session_uuid, since_epoch, max(1, limit))

    if obj.json_output:
        Output(json_mode=True).result(events, "")
        return
    if not events:
        click.echo("no history events")
        return
    for event in events:
        line = f"{format_ts(event['ts'])}  [{event['type']:<10}]"
        if event["type"] == "failure":
            line += (
                f" {event['session']}: #{event['id']} {event['kind']}"
                f" turn={event['turn_id']} status={event['status']}"
            )
        elif event["type"] == "plan":
            line += (
                f" {event['session']}: plan #{event['id']} ({event['strategy']})"
                f" not_before={format_ts(event['not_before'])} status={event['status']}"
            )
        elif event["type"] == "delivery":
            line += (
                f" {event['session']}: delivery #{event['id']}"
                f" attempt={event['attempt_no']} outcome={event['outcome']}"
            )
        else:
            line += (
                f" {event['session']}: {event['from_state'] or '-'} -> {event['to_state']}"
                f" ({event['reason']})"
            )
        click.echo(line)


def register_commands(group: click.Group) -> None:
    group.add_command(history_cmd)
