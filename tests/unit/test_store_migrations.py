"""P2-T2 迁移框架测试:幂等、双写校验、编号纪律(T-STORE 基础)。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codex_autopilot.store import migrations as m
from codex_autopilot.store.migrations import MigrationError

EXPECTED_TABLES = {
    "schema_migrations",
    "runtime_flags",
    "accounts",
    "sessions",
    "session_state",
    "state_history",
    "quota_snapshots",
    "quota_windows",
    "failures",
    "recovery_plans",
    "deliveries",
    "confirmations",
    "notification_log",
    "audit_log",
}


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def migrations() -> tuple[m.Migration, ...]:
    from codex_autopilot.store import _MIGRATIONS_DIR

    return m.discover_migrations(_MIGRATIONS_DIR)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class TestApply:
    def test_fresh_apply_creates_all_tables(self, conn, migrations):
        applied = m.apply_migrations(conn, migrations, now=1000)
        assert applied == [1]
        assert _tables(conn) == EXPECTED_TABLES

    def test_dual_write_user_version_and_table(self, conn, migrations):
        m.apply_migrations(conn, migrations, now=1000)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert [
            tuple(r) for r in conn.execute("SELECT version, applied_at FROM schema_migrations")
        ] == [(1, 1000)]

    def test_reopen_is_idempotent(self, conn, migrations):
        assert m.apply_migrations(conn, migrations, now=1000) == [1]
        assert m.apply_migrations(conn, migrations, now=2000) == []
        assert [tuple(r) for r in conn.execute("SELECT version FROM schema_migrations")] == [(1,)]

    def test_half_migrated_rejected(self, conn, migrations):
        # 表内有版本记录但 user_version 未写(模拟半迁移)
        m.ensure_schema_migrations_table(conn)
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, 1)")
        with pytest.raises(MigrationError, match="half-migrated"):
            m.apply_migrations(conn, migrations, now=1000)

    def test_unknown_future_version_rejected(self, conn, migrations):
        m.ensure_schema_migrations_table(conn)
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (999, 1)")
        conn.execute("PRAGMA user_version = 999")
        with pytest.raises(MigrationError, match="not present in this build"):
            m.apply_migrations(conn, migrations, now=1000)

    def test_failed_migration_rolls_back_atomically(self, conn):
        bad = (
            m.Migration(
                version=1,
                name="0001_ok.sql",
                statements=("CREATE TABLE t1 (id INTEGER PRIMARY KEY)",),
            ),
            m.Migration(
                version=2,
                name="0002_bad.sql",
                statements=(
                    "CREATE TABLE t2 (id INTEGER PRIMARY KEY)",
                    "CREATE TABLE syntax error here",
                ),
            ),
        )
        with pytest.raises(MigrationError, match="0002_bad"):
            m.apply_migrations(conn, bad, now=1000)
        tables = _tables(conn)
        assert "t1" in tables
        assert "t2" not in tables  # 事务回滚
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert m.applied_versions(conn) == {1}


class TestDiscover:
    def test_packaged_migrations_valid(self):
        from codex_autopilot.store import _MIGRATIONS_DIR

        found = m.discover_migrations(_MIGRATIONS_DIR)
        assert [mig.version for mig in found] == list(range(1, len(found) + 1))

    def test_gap_rejected(self, tmp_path: Path):
        (tmp_path / "0001_a.sql").write_text("CREATE TABLE t (id INTEGER)")
        (tmp_path / "0003_b.sql").write_text("CREATE TABLE u (id INTEGER)")
        with pytest.raises(MigrationError, match="contiguous"):
            m.discover_migrations(tmp_path)

    def test_must_start_at_0001(self, tmp_path: Path):
        (tmp_path / "0002_b.sql").write_text("CREATE TABLE u (id INTEGER)")
        with pytest.raises(MigrationError, match="start at version 0001"):
            m.discover_migrations(tmp_path)

    def test_comments_stripped(self, tmp_path: Path):
        sql_file = tmp_path / "0001_c.sql"
        sql_file.write_text(
            "-- a comment with ; semicolon\nCREATE TABLE t (id INTEGER);\n-- trailing\n",
            encoding="utf-8",
        )
        (found,) = m.discover_migrations(tmp_path)
        assert found.statements == ("CREATE TABLE t (id INTEGER)",)
