"""P2-T2 Store 测试:PRAGMA 安全策略、事务纪律、审计(T-STORE 基础)。

`synchronous=FULL` 是 Phase 2 安全复审的结论(persistence.md §2.1),
`test_synchronous_full_is_enforced` 防止任何人无意间降回 NORMAL。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codex_autopilot.store import Store
from codex_autopilot.store.errors import (
    NestedTransactionError,
    StoreError,
    StoreNotOpenError,
)
from codex_autopilot.util.audit import AuditRecordError


class TestOpen:
    def test_creates_state_dir_and_db(self, tmp_path: Path, fake_clock):
        db = tmp_path / "deep" / "nested" / "state.db"
        with Store.open(db, clock=fake_clock) as store:
            assert db.is_file()
            assert store.query_one("SELECT COUNT(*) AS c FROM schema_migrations")["c"] == 1

    def test_reopen_idempotent(self, tmp_path: Path, fake_clock):
        db = tmp_path / "state.db"
        with Store.open(db, clock=fake_clock):
            pass
        with Store.open(db, clock=fake_clock) as store:
            assert store.query_one("SELECT COUNT(*) AS c FROM schema_migrations")["c"] == 1

    def test_wal_mode_active(self, store: Store):
        assert store.query_one("PRAGMA journal_mode")["journal_mode"].lower() == "wal"

    def test_synchronous_full_is_enforced(self, store: Store):
        # 2 == FULL。安全复审结论:NORMAL 会让 attempting 落库在掉电时丢失,
        # 打破投递幂等(FR-05-A5)。任何降级都必须先改 persistence.md §2.1。
        assert store.query_one("PRAGMA synchronous")["synchronous"] == 2

    def test_busy_timeout_and_foreign_keys(self, store: Store):
        assert store.query_one("PRAGMA busy_timeout")[0] == 5000
        assert store.query_one("PRAGMA foreign_keys")["foreign_keys"] == 1

    def test_version_mismatch_wrapped_as_store_error(self, tmp_path: Path, fake_clock):
        db = tmp_path / "state.db"
        with Store.open(db, clock=fake_clock):
            pass
        raw = sqlite3.connect(db)
        raw.execute("PRAGMA user_version = 7")  # 模拟半迁移
        raw.close()
        with pytest.raises(StoreError, match="migration failed"):
            Store.open(db, clock=fake_clock)

    def test_open_on_unwritable_dir_raises_store_error(self, tmp_path: Path, fake_clock):
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        blocked.chmod(0o500)
        try:
            with pytest.raises(StoreError):
                Store.open(blocked / "sub" / "state.db", clock=fake_clock)
        finally:
            blocked.chmod(0o700)


class TestTransactions:
    def test_commit_persists(self, store: Store, fake_clock):
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO accounts(codex_home, created_at) VALUES ('/x', ?)",
                (fake_clock.epoch(),),
            )
        assert store.query_one("SELECT COUNT(*) AS c FROM accounts")["c"] == 1

    def test_rollback_on_exception(self, store: Store, fake_clock):
        with pytest.raises(RuntimeError, match="boom"), store.transaction() as conn:
            conn.execute(
                "INSERT INTO accounts(codex_home, created_at) VALUES ('/x', ?)",
                (fake_clock.epoch(),),
            )
            raise RuntimeError("boom")
        assert store.query_one("SELECT COUNT(*) AS c FROM accounts")["c"] == 0
        assert not store.in_transaction

    def test_rollback_on_keyboard_interrupt(self, store: Store):
        with pytest.raises(KeyboardInterrupt), store.transaction() as conn:
            conn.execute("INSERT INTO runtime_flags(key, value, updated_at) VALUES ('k', '1', 1)")
            raise KeyboardInterrupt
        assert store.query_one("SELECT COUNT(*) AS c FROM runtime_flags")["c"] == 0

    def test_nested_transaction_rejected(self, store: Store):
        with store.transaction(), pytest.raises(NestedTransactionError):
            store.transaction().__enter__()

    def test_connection_reusable_after_rollback(self, store: Store, fake_clock):
        with pytest.raises(RuntimeError), store.transaction() as conn:
            conn.execute("INSERT INTO accounts(codex_home, created_at) VALUES ('/x', 1)")
            raise RuntimeError
        with store.transaction() as conn:
            conn.execute("INSERT INTO accounts(codex_home, created_at) VALUES ('/y', 2)")
        assert store.query_one("SELECT COUNT(*) AS c FROM accounts")["c"] == 1


class TestAudit:
    def test_audit_write(self, store: Store, fake_clock):
        store.audit(
            actor="cli:add",
            category="admin",
            level="info",
            summary="test entry",
            session_uuid="01234567-89ab-cdef-0123-456789abcdef",
            data={"k": "v"},
        )
        row = store.query_one("SELECT * FROM audit_log")
        assert row["actor"] == "cli:add"
        assert row["category"] == "admin"
        assert row["ts"] == fake_clock.epoch()
        assert '"k"' in row["data"]

    def test_audit_rejects_bad_actor(self, store: Store):
        with pytest.raises(AuditRecordError):
            store.audit(actor="evil", category="admin", level="info", summary="x")

    def test_audit_rejects_bad_category(self, store: Store):
        with pytest.raises(AuditRecordError):
            store.audit(actor="daemon", category="nope", level="info", summary="x")


class TestMaintenance:
    def test_integrity_ok(self, store: Store):
        assert store.integrity_ok() is True

    def test_checkpoint_wal(self, store: Store, fake_clock):
        store.audit(actor="daemon", category="tick", level="info", summary="t")
        store.checkpoint_wal()

    def test_closed_store_rejects_use(self, tmp_path: Path, fake_clock):
        store = Store.open(tmp_path / "state.db", clock=fake_clock)
        store.close()
        with pytest.raises(StoreNotOpenError):
            store.query("SELECT 1")
        assert store.closed
