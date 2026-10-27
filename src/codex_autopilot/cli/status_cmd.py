"""`status` 命令:会话/全局状态详情(cli-design.md §3;P4-T5)。

展示:状态机有效状态(含 PAUSED 覆盖)、活跃失败与计划、最近投递与
确认(P4 恒无)、rollout 存在性、tmux 探测(只读;D-SES-4)、账户额度
视图摘要。存在 NEEDS_HUMAN 会话时退出码 10(cli-design §6)。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import click

from codex_autopilot.cli.context import EXIT_ERROR, EXIT_NEEDS_HUMAN, CliContext, CliError
from codex_autopilot.cli.errors import handle_cli_errors
from codex_autopilot.cli.output import Output, format_ts
from codex_autopilot.codexif.discovery import find_rollout_chain
from codex_autopilot.quota.service import QuotaService
from codex_autopilot.recovery.state_machine import effective_state
from codex_autopilot.store.dao import RuntimeFlagsDao, SessionDao
from codex_autopilot.store.recovery_dao import FailureDao, RecoveryPlanDao


def _tmux_alive(session_name: str | None) -> bool | None:
    """tmux has-session 只读探测(D-SES-4:仅展示,不参与任何决策)。"""

    if not session_name:
        return None
    if shutil.which("tmux") is None:
        return None
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", f"={session_name}"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0


def _session_detail(store, config, row) -> dict[str, Any]:
    failures = FailureDao(store)
    plans = RecoveryPlanDao(store)
    state_row = store.query_one(
        "SELECT * FROM session_state WHERE session_uuid = ?", (row["uuid"],)
    )
    failure = failures.open_for_session(row["uuid"])
    needs_human_failure = None
    if failure is None:
        needs_human_failure = store.query_one(
            "SELECT * FROM failures WHERE session_uuid = ? AND status = 'needs_human'"
            " ORDER BY id DESC LIMIT 1",
            (row["uuid"],),
        )
    plan = plans.waiting_for_session(row["uuid"])
    last_delivery = store.query_one(
        "SELECT d.* FROM deliveries d JOIN failures f ON f.id = d.failure_id"
        " WHERE f.session_uuid = ? ORDER BY d.id DESC LIMIT 1",
        (row["uuid"],),
    )
    try:
        chain = find_rollout_chain(row["account_codex_home"], row["uuid"])
    except Exception:
        chain = []
    reference = failure if failure is not None else needs_human_failure
    return {
        "name": row["name"],
        "uuid": row["uuid"],
        "account": {
            "codex_home": row["account_codex_home"],
            "label": row["account_label"],
        },
        "state": effective_state(state_row, enabled=bool(row["enabled"])),
        "state_since": state_row["since"] if state_row is not None else None,
        "enabled": bool(row["enabled"]),
        "auto_resume": bool(row["auto_resume"]),
        "tmux_session": row["tmux_session"],
        "tmux_alive": _tmux_alive(row["tmux_session"]),
        "rollout_present": bool(chain),
        "failure": (
            {
                "id": int(reference["id"]),
                "kind": reference["kind"],
                "detected_at": int(reference["detected_at"]),
                "status": reference["status"],
                "blocking_window_keys": _json_list(reference["blocking_window_keys"]),
            }
            if reference is not None
            else None
        ),
        "plan": (
            {
                "id": int(plan["id"]),
                "strategy": plan["strategy"],
                "not_before": int(plan["not_before"]),
                "status": plan["status"],
            }
            if plan is not None
            else None
        ),
        "last_delivery": (
            {
                "id": int(last_delivery["id"]),
                "outcome": last_delivery["outcome"],
                "started_at": int(last_delivery["started_at"]),
            }
            if last_delivery is not None
            else None
        ),
        "last_event_at": row["last_event_at"],
    }


def _json_list(raw: str | None) -> list:
    try:
        value = json.loads(raw or "[]")
        return value if isinstance(value, list) else []
    except ValueError:
        return []


@click.command(name="status")
@click.argument("name", required=False)
@handle_cli_errors
@click.pass_context
def status_cmd(ctx: click.Context, name: str | None) -> None:
    """Show session/global recovery status (state machine view)."""
    obj: CliContext = ctx.obj
    store = obj.store()
    sessions = SessionDao(store)

    if name is not None:
        row = sessions.resolve(name)
        if row is None:
            raise CliError(f"session not found: {name}", exit_code=EXIT_ERROR, code="not_found")
        rows = [
            store.query_one(
                "SELECT s.*, a.codex_home AS account_codex_home, a.label AS account_label,"
                " st.state AS state, st.since AS state_since,"
                " (SELECT MAX(ts) FROM state_history h WHERE h.session_uuid = s.uuid)"
                "   AS last_event_at"
                " FROM sessions s JOIN accounts a ON a.id = s.account_id"
                " LEFT JOIN session_state st ON st.session_uuid = s.uuid"
                " WHERE s.uuid = ? AND s.deleted_at IS NULL",
                (row["uuid"],),
            )
        ]
        rows = [r for r in rows if r is not None]
    else:
        rows = sessions.list_active()

    quota = QuotaService(store, obj.config, obj.clock)
    global_pause = bool(RuntimeFlagsDao(store).get("global_pause"))
    accounts = quota.monitored_accounts() or quota.all_accounts()
    account_views = [
        {
            "codex_home": account["codex_home"],
            "label": account["label"],
            "view": quota.latest_view(account),
        }
        for account in accounts
    ]

    session_payloads = [_session_detail(store, obj.config, row) for row in rows]
    payload = {
        "global_pause": global_pause,
        "sessions": session_payloads,
        "accounts": [
            {
                "codex_home": entry["codex_home"],
                "label": entry["label"],
                "snapshot_source": (
                    entry["view"].snapshot["source"] if entry["view"].snapshot else None
                ),
                "captured_at": (
                    entry["view"].snapshot["captured_at"] if entry["view"].snapshot else None
                ),
                "stale": entry["view"].stale,
                "blocked": entry["view"].assessment.blocked,
                "blocked_kind": entry["view"].assessment.blocked_kind,
            }
            for entry in account_views
        ],
    }

    if obj.json_output:
        Output(json_mode=True).result(payload, "")
    else:
        click.echo(f"global pause: {'on' if global_pause else 'off'}")
        if not session_payloads:
            click.echo("no registered sessions")
        for entry in session_payloads:
            click.echo("")
            click.echo(
                f"session: {entry['name']} ({entry['uuid']})"
                f"  state: {entry['state']}"
                f"  since: {format_ts(entry['state_since'])}"
            )
            click.echo(
                f"  account: {entry['account']['codex_home']}"
                f"  enabled: {entry['enabled']}  auto_resume: {entry['auto_resume']}"
            )
            click.echo(
                f"  rollout: {'present' if entry['rollout_present'] else 'MISSING'}"
                + (
                    f"  tmux: {entry['tmux_session']}"
                    f" ({'alive' if entry['tmux_alive'] else 'not alive'})"
                    if entry["tmux_session"]
                    else ""
                )
            )
            failure = entry["failure"]
            if failure:
                click.echo(
                    f"  failure #{failure['id']}: {failure['kind']}"
                    f" ({failure['status']}) detected {format_ts(failure['detected_at'])}"
                )
            plan = entry["plan"]
            if plan:
                click.echo(
                    f"  plan #{plan['id']}: {plan['strategy']}"
                    f" not_before {format_ts(plan['not_before'])} ({plan['status']})"
                )
            delivery = entry["last_delivery"]
            if delivery:
                click.echo(
                    f"  last delivery #{delivery['id']}: {delivery['outcome']}"
                    f" at {format_ts(delivery['started_at'])}"
                )
        for entry in payload["accounts"]:
            click.echo("")
            label = entry["label"] or entry["codex_home"]
            state = (
                f"{entry['snapshot_source']} {'stale' if entry['stale'] else 'fresh'}"
                if entry["snapshot_source"]
                else "no data"
            )
            click.echo(
                f"account: {label}  quota: {state}"
                f"  blocked: {entry['blocked']}"
                f" ({entry['blocked_kind']})"
            )

    if any(s["state"] == "NEEDS_HUMAN" for s in session_payloads):
        raise click.exceptions.Exit(EXIT_NEEDS_HUMAN)


def register_commands(group: click.Group) -> None:
    group.add_command(status_cmd)
