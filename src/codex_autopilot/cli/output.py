"""输出契约:--json 时 stdout 仅一行 JSON,人类可读信息走 stderr(cli-design §1)。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import click


class Output:
    """命令输出的统一出口。

    - `result(payload, human_text)`:--json 输出一行 JSON 到 stdout;
      否则人类可读文本到 stdout;
    - `warn/info`:诊断信息,一律 stderr。
    """

    def __init__(self, *, json_mode: bool):
        self.json_mode = json_mode

    def result(self, payload: Any, human_text: str) -> None:
        if self.json_mode:
            click.echo(json.dumps(payload, ensure_ascii=False, default=str))
        else:
            click.echo(human_text.rstrip("\n"))

    def warn(self, text: str) -> None:
        click.echo(f"warning: {text}", err=True)

    def info(self, text: str) -> None:
        click.echo(text, err=True)


def emit_error(*, json_mode: bool, code: str, message: str) -> None:
    """错误输出:JSON 模式 stdout 一行错误对象;两种模式 stderr 都有人读信息。"""

    click.echo(f"error: {message}", err=True)
    if json_mode:
        click.echo(json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False))


def format_ts(ts: int | None) -> str:
    """Unix 秒 → 本地时间 + UTC 偏移注记(cli-design §4:本地为主,并注偏移)。"""

    if ts is None:
        return "-"
    local = datetime.fromtimestamp(ts).astimezone()
    offset = local.utcoffset()
    if offset is None:  # pragma: no cover - astimezone() 后总有时区
        return local.strftime("%Y-%m-%d %H:%M:%S")
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return (
        local.strftime("%Y-%m-%d %H:%M:%S")
        + f" ({sign}{total // 3600:02d}{(total % 3600) // 60:02d})"
    )
