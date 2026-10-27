"""编号迁移框架(persistence.md §6)。

- 每个迁移一个 `NNNN_描述.sql`,从 0001 起连续编号,只增不改;
- 应用迁移 = 每个迁移一个 IMMEDIATE 事务,DDL + `schema_migrations` 行 +
  `PRAGMA user_version` 同事务写入(双写,防半迁移状态);
- 打开数据库时校验 `user_version` 与表内版本一致,不一致即拒绝打开
  (半迁移/外部工具动过库 → 受限,交人工处理);
- 迁移 SQL 限制:纯 DDL/DML 语句,分号分隔;不支持触发器等含字符串分号的
  结构(分割器按语句拆分,见 `_split_statements`)。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_VERSION_RE = re.compile(r"^(\d{4})_\w+\.sql$")


class MigrationError(Exception):
    """迁移文件非法、编号断裂或数据库处于不一致版本状态。"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


def _split_statements(sql_text: str) -> tuple[str, ...]:
    """去掉 `--` 行注释并按分号拆分(迁移文件约定不含字符串内分号)。"""

    lines = [line.split("--", 1)[0] for line in sql_text.splitlines()]
    stripped = "\n".join(lines)
    statements = [stmt.strip() for stmt in stripped.split(";")]
    return tuple(stmt for stmt in statements if stmt)


def discover_migrations(directory: Path) -> tuple[Migration, ...]:
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")
    migrations: list[Migration] = []
    for path in sorted(directory.iterdir()):
        match = _VERSION_RE.fullmatch(path.name)
        if not match:
            continue  # 忽略 README 等非迁移文件
        migrations.append(
            Migration(
                version=int(match.group(1)),
                name=path.name,
                statements=_split_statements(path.read_text(encoding="utf-8")),
            )
        )
    versions = [m.version for m in migrations]
    if not migrations or versions[0] != 1:
        raise MigrationError("migrations must start at version 0001")
    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        raise MigrationError(f"migration versions must be contiguous from 0001, found {versions}")
    return tuple(migrations)


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return {int(row[0]) for row in rows}


def ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )


def apply_migrations(
    conn: sqlite3.Connection, migrations: tuple[Migration, ...], *, now: int
) -> list[int]:
    """应用全部未应用的迁移(依序);返回本次应用的版本列表(幂等)。"""

    ensure_schema_migrations_table(conn)
    done = applied_versions(conn)
    known = {m.version for m in migrations}
    unknown = sorted(done - known)
    if unknown:
        raise MigrationError(
            f"database contains migration versions not present in this build: {unknown}; "
            "refusing to open (downgrade is not supported; restore from backup, persistence.md §9)"
        )
    fresh_user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    max_applied = max(done) if done else 0
    if fresh_user_version != max_applied:
        raise MigrationError(
            f"schema version mismatch: PRAGMA user_version={fresh_user_version} but "
            f"schema_migrations max={max_applied}; database may be half-migrated"
        )
    applied_now: list[int] = []
    for migration in migrations:
        if migration.version in done:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (migration.version, now),
            )
            conn.execute(f"PRAGMA user_version = {migration.version}")
        except sqlite3.Error as exc:
            conn.execute("ROLLBACK")
            raise MigrationError(f"migration {migration.name} failed: {exc}") from exc
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        applied_now.append(migration.version)
    return applied_now
