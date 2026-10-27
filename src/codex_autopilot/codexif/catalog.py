"""Codex 会话目录汇总:`list --codex-sessions` 的数据面(P4-T1;只读)。

- 按链尾 UUID 聚合 rollout 文件(分裂链归并到链尾会话);
- 每会话:起始时间(文件名)、最后事件时间(解析)、行数(物理行)、
  cwd(首个 session_meta)、链、是否 archived;
- `local_thread_catalog` 交叉(sqlite/codex.db,尽力而为):
  列名跨版本未核实(phase0 §2.4),按候选列名探测,任何失败按"无目录"
  处理(返回 None,展示层标注 unknown)。

红线:全部只读;SQLite 一律 mode=ro 打开(recovery §4.1 读取纪律)。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from codex_autopilot.codexif.discovery import SessionFile, discover_session_files
from codex_autopilot.codexif.rollout import read_file_events

logger = logging.getLogger(__name__)

# local_thread_catalog 的候选 UUID 列名(跨版本防御;命中第一个存在的)
_CATALOG_UUID_COLUMNS = ("thread_uuid", "uuid", "thread_id", "id")


@dataclass(frozen=True)
class CodexSessionSummary:
    uuid: str
    started_at: int | None  # 文件名时间戳(本地语义 → epoch;缺失 None)
    last_event_at: int | None
    line_count: int
    cwd: str | None
    chain: tuple[str, ...]
    archived: bool
    file_count: int


def _filename_ts_to_epoch(started: datetime | None) -> int | None:
    if started is None:
        return None
    return int(started.timestamp())


def _count_lines(path: Path) -> int:
    count = 0
    try:
        with path.open("rb") as handle:
            for _ in handle:
                count += 1
    except OSError:
        return 0
    return count


def summarize_sessions(
    codex_home: Path,
    *,
    days: int = 30,
    include_archived: bool = True,
) -> list[CodexSessionSummary]:
    """扫描 Codex Home,汇总最近 N 天的会话(只读;按 started_at 倒序)。"""

    home = Path(codex_home)
    files = discover_session_files(home, include_archived=include_archived)
    cutoff = datetime.now() - timedelta(days=days)
    grouped: dict[str, list[SessionFile]] = {}
    for session_file in files:
        # days 过滤:命名时间缺失的条目保留(标注 unknown,不静默丢弃)
        if session_file.started_at is not None and session_file.started_at < cutoff:
            continue
        grouped.setdefault(session_file.uuid, []).append(session_file)

    summaries: list[CodexSessionSummary] = []
    for uuid, group in grouped.items():
        last_event_at: int | None = None
        cwd: str | None = None
        line_count = 0
        for session_file in group:
            line_count += _count_lines(session_file.path)
            try:
                events = read_file_events(session_file.path)
            except Exception:
                logger.warning("cannot parse rollout for summary: %s", session_file.path)
                continue
            for event in events:
                if event.timestamp is not None and (
                    last_event_at is None or event.timestamp > last_event_at
                ):
                    last_event_at = event.timestamp
                if cwd is None and event.type == "session_meta":
                    value = event.payload.get("cwd")
                    if isinstance(value, str) and value:
                        cwd = value
        summaries.append(
            CodexSessionSummary(
                uuid=uuid,
                started_at=_filename_ts_to_epoch(group[-1].started_at),
                last_event_at=last_event_at,
                line_count=line_count,
                cwd=cwd,
                chain=group[-1].chain,
                archived=group[-1].archived,
                file_count=len(group),
            )
        )
    summaries.sort(key=lambda s: (s.last_event_at is None, s.last_event_at or 0), reverse=True)
    return summaries


def thread_catalog_uuids(codex_home: Path) -> set[str] | None:
    """`sqlite/codex.db` 的 local_thread_catalog UUID 集合;无库/不可读 → None。"""

    db_path = Path(codex_home) / "sqlite" / "codex.db"
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        conn.execute("PRAGMA query_only=ON")
        columns = [row[1] for row in conn.execute("PRAGMA table_info(local_thread_catalog)")]
        if not columns:
            return None
        uuid_column = next((name for name in _CATALOG_UUID_COLUMNS if name in columns), None)
        if uuid_column is None:
            return None
        return {
            row[0]
            for row in conn.execute(f"SELECT {uuid_column} FROM local_thread_catalog")
            if isinstance(row[0], str)
        }
    except sqlite3.Error as exc:
        logger.warning("thread catalog cross-check failed: %s", exc)
        return None
    finally:
        conn.close()


def session_meta_cwd(path: Path) -> str | None:
    """单文件首个 session_meta 的 cwd(内部辅助,测试用)。"""

    try:
        for event in read_file_events(path):
            if event.type == "session_meta":
                value = event.payload.get("cwd")
                if isinstance(value, str) and value:
                    return value
    except Exception:
        return None
    return None


__all__ = [
    "CodexSessionSummary",
    "session_meta_cwd",
    "summarize_sessions",
    "thread_catalog_uuids",
]
