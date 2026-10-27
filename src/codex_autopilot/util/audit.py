"""审计写入(append-only,NFR-06 / persistence.md §4)。

唯一被允许触碰 `audit_log` 表的写入路径:**仅 INSERT**,没有任何
UPDATE/DELETE 语句;由测试 T-STORE-03(源码扫描)锁定。
该模块只依赖 sqlite3 连接,不依赖 Store,便于任何层复用。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

VALID_ACTORS_PREFIXES = ("daemon", "cli:", "recovery:")
VALID_CATEGORIES = frozenset({"tick", "quota", "state", "delivery", "notify", "fault", "admin"})
VALID_LEVELS = frozenset({"info", "warn", "error"})


class AuditRecordError(ValueError):
    """审计记录字段不合法(调用方 bug,应视为编程错误)。"""


def write_audit(
    conn: sqlite3.Connection,
    *,
    ts: int,
    actor: str,
    category: str,
    level: str,
    summary: str,
    session_uuid: str | None = None,
    data: dict[str, Any] | None = None,
) -> int:
    """插入一条审计记录并返回行 id。必须在调用方的事务内执行。"""

    if not (actor.startswith(VALID_ACTORS_PREFIXES)):
        raise AuditRecordError(f"invalid audit actor {actor!r}")
    if category not in VALID_CATEGORIES:
        raise AuditRecordError(f"invalid audit category {category!r}")
    if level not in VALID_LEVELS:
        raise AuditRecordError(f"invalid audit level {level!r}")
    if not summary.strip():
        raise AuditRecordError("audit summary must be non-empty")
    cursor = conn.execute(
        "INSERT INTO audit_log(ts, actor, category, level, session_uuid, summary, data) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            int(ts),
            actor,
            category,
            level,
            session_uuid,
            summary,
            json.dumps(data, ensure_ascii=False, default=str) if data is not None else None,
        ),
    )
    return int(cursor.lastrowid)
