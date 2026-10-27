"""结构化 JSON 日志(architecture.md §8:stdlib logging + JSON formatter,不引第三方)。

JSON 行输出到 stderr(journald/前台均可采集);stdout 保留给 CLI 机器可读输出
(cli-design.md §1:`--json` 时 stdout 仅一行 JSON)。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

# record.__dict__ 中属于 logging 框架自身的键;其余视为调用方传入的结构化字段。
_RESERVED_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """把 LogRecord 序列化为一行 JSON。

    固定字段:`ts`(ISO 8601 UTC)、`level`、`logger`、`message`;
    额外字段通过 `logger.info(..., extra={"session_uuid": ...})` 传入;
    异常附带 `exception` 文本。约定:不得把会话正文写入日志(NFR-09)。
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")
        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(
    level: int = logging.INFO,
    *,
    stream: TextIO | None = None,
    root_logger_name: str = "codex_autopilot",
) -> None:
    """配置 `codex_autopilot` 层级日志为 JSON 输出;幂等,可重复调用。"""

    logger = logging.getLogger(root_logger_name)
    logger.setLevel(level)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False
