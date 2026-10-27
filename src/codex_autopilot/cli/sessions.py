"""会话注册命令:add / remove / list(cli-design.md §3,P2-T4)。

`list --codex-sessions`(P4-T1):只读扫描 Codex Home 汇总可注册会话,
含分裂链归并与 local_thread_catalog 交叉(session-management §6)。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import click

from codex_autopilot.cli.context import EXIT_ERROR, EXIT_USAGE, CliContext, CliError
from codex_autopilot.cli.errors import handle_cli_errors
from codex_autopilot.cli.output import Output, format_ts
from codex_autopilot.codexif.catalog import summarize_sessions, thread_catalog_uuids
from codex_autopilot.quota.service import QuotaService
from codex_autopilot.store.dao import AccountDao, NewSession, SessionDao
from codex_autopilot.store.errors import ConstraintViolationError
from codex_autopilot.util.audit import write_audit
from codex_autopilot.util.identifiers import (
    InvalidIdentifierError,
    canonical_uuid,
    validate_session_name,
)


def _usage_error(message: str, *, code: str) -> CliError:
    return CliError(message, exit_code=EXIT_USAGE, code=code)


def _parse_notify_policy(raw: str | None) -> dict[str, Any] | None:
    """--notify-policy 接受内联 JSON 或 @file;必须是 JSON 对象。"""

    if raw is None:
        return None
    if raw.startswith("@"):
        try:
            text = Path(raw[1:]).read_text(encoding="utf-8")
        except OSError as exc:
            raise _usage_error(
                f"cannot read notify policy file {raw[1:]}: {exc}", code="notify_policy"
            ) from exc
    else:
        text = raw
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _usage_error(
            f"--notify-policy is not valid JSON: {exc}", code="notify_policy"
        ) from exc
    if not isinstance(parsed, dict):
        raise _usage_error("--notify-policy must be a JSON object", code="notify_policy")
    return parsed


@click.command(name="add")
@click.option(
    "--session", "session_uuid", required=True, help="Session UUID (canonical 8-4-4-4-12)."
)
@click.option("--name", required=True, help="Unique session name ^[a-z][a-z0-9-]{0,63}$.")
@click.option("--description", default=None, help="Optional human-readable description.")
@click.option(
    "--project",
    "project",
    type=click.Path(path_type=Path),
    default=None,
    help="Project path (default: current directory).",
)
@click.option(
    "--codex-home",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Codex Home directory (default: config codex.codex_home).",
)
@click.option(
    "--tmux-session", default=None, help="Optional tmux session name (informational only)."
)
@click.option("--resume-template", default=None, help="Per-session resume message template.")
@click.option(
    "--check-interval", type=int, default=None, help="Per-session check interval in seconds."
)
@click.option(
    "--auto-resume",
    is_flag=True,
    default=False,
    help="Allow automatic resume for this session (default OFF: monitor-only).",
)
@click.option("--notify-policy", default=None, help="Notify policy as inline JSON or @file.")
@click.option(
    "--allow-missing-project",
    is_flag=True,
    help="Downgrade missing project path from error to warning.",
)
@handle_cli_errors
@click.pass_context
def add(
    ctx: click.Context,
    session_uuid: str,
    name: str,
    description: str | None,
    project: Path | None,
    codex_home: Path | None,
    tmux_session: str | None,
    resume_template: str | None,
    check_interval: int | None,
    auto_resume: bool,
    notify_policy: str | None,
    allow_missing_project: bool,
) -> None:
    """Register a session for monitoring (session-management §2.1)."""
    obj: CliContext = ctx.obj
    out = Output(json_mode=obj.json_output)

    # 1) 标识符与标量校验(FR-01-A1/A3;错误 = 用法错误)
    try:
        canonical = canonical_uuid(session_uuid)
    except InvalidIdentifierError as exc:
        raise _usage_error(str(exc), code="invalid_uuid") from exc
    try:
        validate_session_name(name)
    except InvalidIdentifierError as exc:
        raise _usage_error(str(exc), code="invalid_name") from exc
    if check_interval is not None and check_interval < 1:
        raise _usage_error("--check-interval must be >= 1 second", code="invalid_check_interval")
    policy = _parse_notify_policy(notify_policy)

    # 2) Codex Home:显式 > 配置默认;必须存在(FR-01-A2);realpath 规范化(§1.2)
    effective_home = Path(codex_home) if codex_home is not None else obj.config.codex_home
    if not effective_home.expanduser().is_dir():
        raise _usage_error(
            f"codex home does not exist or is not a directory: {effective_home}",
            code="codex_home_missing",
        )
    normalized_home = os.path.realpath(effective_home.expanduser())

    # 3) 项目路径:默认当前目录;缺失默认拒绝,--allow-missing-project 降级警告
    project_path = Path(project) if project is not None else Path.cwd()
    if not project_path.is_dir():
        message = f"project path does not exist or is not a directory: {project_path}"
        if allow_missing_project:
            out.warn(f"{message}; registered anyway (--allow-missing-project)")
        else:
            raise _usage_error(message, code="project_missing")

    # 4) 单事务写入:账户 get_or_create + 会话 + 审计(FR-01/§2.1 步骤 6)
    store = obj.store()
    accounts, sessions = AccountDao(store), SessionDao(store)
    now = obj.clock.epoch()
    try:
        with store.transaction() as conn:
            account = accounts.get_or_create(normalized_home, label=None, now=now)
            record = NewSession(
                uuid=canonical,
                name=name,
                description=description,
                project_path=os.path.realpath(project_path),
                account_id=int(account["id"]),
                auto_resume=auto_resume,
                tmux_session=tmux_session,
                resume_template=resume_template,
                notify_policy=policy,
                check_interval=check_interval,
            )
            session = sessions.insert(record, now=now)
            write_audit(
                conn,
                ts=now,
                actor="cli:add",
                category="admin",
                level="info",
                summary=f"registered session {name} ({canonical})",
                session_uuid=canonical,
                data={"codex_home": normalized_home, "auto_resume": auto_resume},
            )
    except ConstraintViolationError as exc:
        raise CliError(
            _constraint_message(exc, canonical, name),
            exit_code=EXIT_ERROR,
            code=exc.key,
        ) from exc

    payload = {
        "name": session["name"],
        "uuid": session["uuid"],
        "description": session["description"],
        "project_path": session["project_path"],
        "codex_home": account["codex_home"],
        "account_label": account["label"],
        "enabled": bool(session["enabled"]),
        "auto_resume": bool(session["auto_resume"]),
        "tmux_session": session["tmux_session"],
        "check_interval": session["check_interval"],
        "created_at": session["created_at"],
    }
    human = (
        f"registered session '{session['name']}' ({session['uuid']})\n"
        f"  account: {account['codex_home']}\n"
        f"  project: {session['project_path']}\n"
        f"  auto_resume: {'on' if session['auto_resume'] else 'off'}"
    )
    if not session["auto_resume"]:
        human += " (safe default: monitor-only, the tool will never deliver messages)"
    out.result(payload, human)


def _constraint_message(exc: ConstraintViolationError, uuid: str, name: str) -> str:
    if exc.key == "duplicate_uuid":
        return f"session UUID {uuid} is already registered"
    if exc.key == "duplicate_name":
        return f"session name '{name}' is already in use"
    return str(exc)


@click.command(name="remove")
@click.argument("name_or_uuid")
@click.option("--purge", is_flag=True, help="Physically delete the session and its history.")
@click.option("--yes", is_flag=True, help="Required confirmation for --purge.")
@handle_cli_errors
@click.pass_context
def remove(ctx: click.Context, name_or_uuid: str, purge: bool, yes: bool) -> None:
    """Remove a session (soft delete by default; history kept)."""
    obj: CliContext = ctx.obj
    store = obj.store()
    sessions = SessionDao(store)
    row = sessions.resolve(name_or_uuid)
    if row is None:
        raise CliError(f"session not found: {name_or_uuid}", exit_code=EXIT_ERROR, code="not_found")
    if row["deleted_at"] is not None:
        raise CliError(
            f"session already removed: {row['name']} (use --purge to delete permanently)",
            exit_code=EXIT_ERROR,
            code="already_removed",
        )
    now = obj.clock.epoch()
    uuid_, name_ = row["uuid"], row["name"]
    with store.transaction() as conn:
        if purge:
            if not yes:
                raise _usage_error(
                    "--purge permanently deletes history; pass --yes to confirm",
                    code="purge_confirmation_required",
                )
            sessions.purge(uuid_)
            write_audit(
                conn,
                ts=now,
                actor="cli:remove",
                category="admin",
                level="warn",
                summary=f"purged session {name_} ({uuid_}) including failure/delivery history",
                session_uuid=uuid_,
            )
            message = f"purged session '{name_}' (records deleted permanently)"
        else:
            sessions.soft_delete(uuid_, now=now)
            write_audit(
                conn,
                ts=now,
                actor="cli:remove",
                category="admin",
                level="info",
                summary=f"removed session {name_} ({uuid_}) (soft delete)",
                session_uuid=uuid_,
            )
            message = f"removed session '{name_}' (soft delete; history kept)"
    Output(json_mode=obj.json_output).result(
        {"name": name_, "uuid": uuid_, "purged": purge}, message
    )


@click.command(name="list")
@click.option(
    "--codex-sessions",
    "codex_sessions",
    is_flag=True,
    help="Scan Codex Home for registrable sessions (read-only; P4-T1).",
)
@click.option(
    "--account",
    "account_arg",
    default=None,
    help="With --codex-sessions: restrict scan to one account (label or codex_home path).",
)
@click.option(
    "--days",
    type=int,
    default=30,
    show_default=True,
    help="With --codex-sessions: only sessions started within N days.",
)
@handle_cli_errors
@click.pass_context
def list_cmd(
    ctx: click.Context,
    codex_sessions: bool,
    account_arg: str | None,
    days: int,
) -> None:
    """List registered sessions (columns per cli-design.md §3)."""
    obj: CliContext = ctx.obj
    if codex_sessions:
        _list_codex_sessions(obj, account_arg, days)
        return
    rows = SessionDao(obj.store()).list_active()
    payload = [
        {
            "name": row["name"],
            "uuid": row["uuid"],
            "account": {"codex_home": row["account_codex_home"], "label": row["account_label"]},
            "enabled": bool(row["enabled"]),
            "auto_resume": bool(row["auto_resume"]),
            "state": row["state"],
            "state_since": row["state_since"],
            "last_event_at": row["last_event_at"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    if obj.json_output:
        Output(json_mode=True).result(payload, "")
        return
    if not rows:
        click.echo("no registered sessions (use 'codex-autopilot add')")
        return
    header = ("NAME", "UUID", "ACCOUNT", "ENABLED", "AUTO_RESUME", "STATE", "LAST_EVENT")
    table = [header]
    for row in rows:
        account = row["account_label"] or row["account_codex_home"]
        table.append(
            (
                row["name"],
                row["uuid"],
                str(account),
                "true" if row["enabled"] else "false",
                "true" if row["auto_resume"] else "false",
                row["state"] or "-",
                format_ts(row["last_event_at"]),
            )
        )
    widths = [max(len(line[col]) for line in table) for col in range(len(header))]
    for index, line in enumerate(table):
        click.echo("  ".join(line[col].ljust(widths[col]) for col in range(len(header))))
        if index == 0:
            click.echo("  ".join("-" * width for width in widths))


def register_commands(group: click.Group) -> None:
    group.add_command(add)
    group.add_command(remove)
    group.add_command(list_cmd)


# -- list --codex-sessions(P4-T1) -----------------------------------------


def _scan_accounts(obj: CliContext, account_arg: str | None) -> list[Any]:
    """选择要扫描的账户:显式 --account > 有监控会话的账户 > 配置默认。"""

    service = QuotaService(obj.store(), obj.config, obj.clock)
    if account_arg is not None:
        row = service.resolve_account(account_arg)
        if row is None:
            raise CliError(
                f"account not found: {account_arg} (match by label or codex_home)",
                exit_code=EXIT_ERROR,
                code="account_not_found",
            )
        return [row]
    accounts = service.monitored_accounts()
    if accounts:
        return accounts
    return service.all_accounts() or [
        AccountDao(obj.store()).get_or_create(
            os.path.realpath(obj.config.codex_home), label=None, now=obj.clock.epoch()
        )
    ]


def _list_codex_sessions(obj: CliContext, account_arg: str | None, days: int) -> None:
    if days < 1:
        raise _usage_error("--days must be >= 1", code="invalid_days")
    out = Output(json_mode=obj.json_output)
    accounts = _scan_accounts(obj, account_arg)
    registered = {
        row["uuid"]
        for row in obj.store().query("SELECT uuid FROM sessions WHERE deleted_at IS NULL")
    }

    payload: list[dict[str, Any]] = []
    for account in accounts:
        home = Path(account["codex_home"])
        catalog = thread_catalog_uuids(home)
        if catalog is None and home.joinpath("sqlite", "codex.db").exists():
            out.warn(f"thread catalog present but unreadable for {home}")
        for summary in summarize_sessions(home, days=days):
            payload.append(
                {
                    "account": {
                        "codex_home": account["codex_home"],
                        "label": account["label"],
                    },
                    "uuid": summary.uuid,
                    "started_at": summary.started_at,
                    "last_event_at": summary.last_event_at,
                    "line_count": summary.line_count,
                    "cwd": summary.cwd,
                    "chain": list(summary.chain),
                    "file_count": summary.file_count,
                    "archived": summary.archived,
                    "registered": summary.uuid in registered,
                    "in_thread_catalog": (summary.uuid in catalog if catalog is not None else None),
                }
            )

    if obj.json_output:
        out.result(payload, "")
        return
    if not payload:
        click.echo(
            "no codex sessions found (scan window: "
            f"{days} days; check codex_home paths or increase --days)"
        )
        return
    header = ("UUID", "STARTED", "LAST_EVENT", "LINES", "ARCH", "REG", "CATALOG", "CWD")
    table: list[tuple[str, ...]] = [header]
    for row in payload:
        cwd = row["cwd"] or "-"
        if len(cwd) > 36:
            cwd = cwd[:33] + "..."
        table.append(
            (
                row["uuid"],
                format_ts(row["started_at"]),
                format_ts(row["last_event_at"]),
                str(row["line_count"]),
                "yes" if row["archived"] else "-",
                "yes" if row["registered"] else "-",
                {True: "yes", False: "-", None: "n/a"}[row["in_thread_catalog"]],
                cwd,
            )
        )
    widths = [max(len(line[col]) for line in table) for col in range(len(header))]
    for index, line in enumerate(table):
        click.echo("  ".join(line[col].ljust(widths[col]) for col in range(len(header))))
        if index == 0:
            click.echo("  ".join("-" * width for width in widths))
