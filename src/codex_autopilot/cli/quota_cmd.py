"""`quota` 命令:额度查询与展示(cli-design.md §3;FR-04、T-CLI-QUOTA)。

语义:
- 默认展示各账户最近快照(不强制刷新;`--refresh` 触发一次 App Server 实时查询);
- 实时查询失败 → 降级展示库存数据(S-08),来源徽标明确标注历史/非实时;
- 每窗口输出强制携带 source/captured_at/stale(FR-04-A2);
- 本命令只读 + 采集落库;不触碰任何恢复状态机(Phase 3 边界)。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import click

from codex_autopilot.cli.context import EXIT_ERROR, CliContext, CliError
from codex_autopilot.cli.errors import handle_cli_errors
from codex_autopilot.cli.output import Output, format_ts
from codex_autopilot.quota.service import QuotaService
from codex_autopilot.store.dao import AccountDao

# 窗口展示顺序:五小时 → 每周 → 其余(credits/unknown 等)按 key
_KIND_ORDER = {"five_hour": 0, "weekly": 1}


def _fmt_percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:g}%"


def _bar(used_percent: float | None, width: int = 10) -> str:
    if used_percent is None:
        return "░" * width
    filled = max(0, min(width, round(used_percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def _sort_windows(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        windows,
        key=lambda w: (
            _KIND_ORDER.get(w["window_kind"], 9),
            w["bucket_key"],
            w.get("window_minutes") or 0,
        ),
    )


def _window_json(account_view, window: dict[str, Any]) -> dict[str, Any]:
    return {
        "account": {"codex_home": account_view.codex_home, "label": account_view.label},
        "source": account_view.snapshot["source"],
        "captured_at": account_view.snapshot["captured_at"],
        "stale": account_view.stale,
        "window_kind": window["window_kind"],
        "inferred": window["kind_inferred"],
        "used_percent": window["used_percent"],
        "resets_at": window["resets_at"],
        "window_minutes": window["window_minutes"],
        "exhausted": window["is_exhausted"],
    }


def _render_account(view) -> list[str]:
    label = view.label or Path(view.codex_home).name
    lines = [f"account: {label} ({view.codex_home})"]
    if view.snapshot is None:
        lines.append("  no quota data yet (try --refresh, or run sessions to produce history)")
        return lines
    snapshot = view.snapshot
    source_label = (
        "appserver (realtime)" if snapshot["source"] == "appserver" else "jsonl (historical)"
    )
    freshness = "stale" if view.stale else "fresh"
    lines.append(
        f"  source: {source_label}   captured: {format_ts(snapshot['captured_at'])}   {freshness}"
    )
    windows = _sort_windows(snapshot["windows"])
    if not windows:
        lines.append("  (snapshot parsed but contains no usable windows)")
        return lines
    for window in windows:
        used = window["used_percent"]
        left = f"{100 - used:g}%" if used is not None else "n/a"
        resets = format_ts(window["resets_at"]) if window["resets_at"] is not None else "-"
        minutes = ""
        if window["window_minutes"]:
            minutes = f" [window_minutes={window['window_minutes']}]"
        marks = ""
        if window["is_exhausted"]:
            marks += " EXHAUSTED"
        if window["kind_inferred"]:
            marks += " (inferred)"
        lines.append(
            f"  {window['window_kind']:<10} {_bar(used)} {_fmt_percent(used)} used"
            f" ({left} left)   resets: {resets}{minutes}{marks}"
        )
    return lines


def _select_accounts(obj: CliContext, account_arg: str | None, out: Output) -> list:
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
    if not accounts:
        # 无监控会话(未注册/已全部软删除):退回全部已知账户——查询只读无害,
        # 且首跑 quota 自动建立的默认账户在 remove 后仍可继续查看
        accounts = service.all_accounts()
        if accounts:
            out.info("no monitored sessions; querying all known accounts")
    if not accounts:
        # 空库:对配置默认 Codex Home 建立账户(get_or_create),quota 可独立于注册使用
        normalized = os.path.realpath(obj.config.codex_home)
        account = AccountDao(obj.store()).get_or_create(
            normalized, label=None, now=obj.clock.epoch()
        )
        out.info(f"no registered sessions; using default codex home {normalized}")
        return [account]
    return accounts


@click.command(name="quota")
@click.option(
    "--account",
    "account_arg",
    default=None,
    help="Restrict to one account (label or codex_home path). Default: all monitored accounts.",
)
@click.option(
    "--refresh",
    is_flag=True,
    help="Trigger one realtime App Server query per selected account (read-only).",
)
@click.option(
    "--source",
    type=click.Choice(["appserver", "jsonl", "auto"]),
    default="auto",
    help="Data source filter (auto = newest ok snapshot of any source).",
)
@handle_cli_errors
@click.pass_context
def quota_cmd(ctx: click.Context, account_arg: str | None, refresh: bool, source: str) -> None:
    """Show quota usage per account with source/freshness labels (quota-monitoring §7)."""
    obj: CliContext = ctx.obj
    out = Output(json_mode=obj.json_output)
    accounts = _select_accounts(obj, account_arg, out)
    service = QuotaService(obj.store(), obj.config, obj.clock)

    if refresh and source == "jsonl":
        out.warn("--refresh queries the App Server; it has no effect with --source jsonl")

    json_rows: list[dict[str, Any]] = []
    human_blocks: list[str] = []
    for account in accounts:
        if source == "appserver" and refresh:
            snapshot = service.refresh_appserver(account, actor="cli:quota")
            if not snapshot.ok:
                out.warn(
                    f"realtime query failed for {account['codex_home']}: {snapshot.error};"
                    " showing stored data, marked non-realtime"
                )
        elif source == "jsonl":
            service.ingest_jsonl(account)
        elif source == "auto" and refresh:
            snapshot = service.refresh_appserver(account, actor="cli:quota")
            if not snapshot.ok:
                out.warn(
                    f"realtime query failed for {account['codex_home']}: {snapshot.error};"
                    " falling back to stored data, marked non-realtime"
                )

        source_filter = None if source == "auto" else source
        view = service.latest_view(account, source=source_filter)

        if view.snapshot is None and source == "auto":
            # 库内无任何快照:本地读 rollout 历史(降级,天然标注 jsonl 来源)
            service.ingest_jsonl(account)
            view = service.latest_view(account, source=None)
        if view.snapshot is not None and view.snapshot["source"] == "jsonl" and not refresh:
            error = service.latest_appserver_error(int(account["id"]))
            if error is not None:
                out.warn(
                    f"last realtime query failed at {format_ts(error['captured_at'])}"
                    f" for {account['codex_home']}: {error['error']}"
                    " (showing historical data)"
                )

        human_blocks.append("\n".join(_render_account(view)))
        if view.snapshot is not None:
            json_rows.extend(_window_json(view, window) for window in view.snapshot["windows"])

    if obj.json_output:
        Output(json_mode=True).result(json_rows, "")
    else:
        click.echo("\n\n".join(human_blocks) if human_blocks else "no accounts to query")


def register_commands(group: click.Group) -> None:
    group.add_command(quota_cmd)
