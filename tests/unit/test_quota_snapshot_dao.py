"""P3 测试:额度快照 DAO(入库/去重/截断/查询,persistence §3/§8)。"""

from __future__ import annotations

import sqlite3

import pytest

from codex_autopilot.store.dao import NewQuotaSnapshot, NewQuotaWindow, QuotaSnapshotDao


def make_snapshot(source="appserver", captured_at=1000, ok=True, raw=None):
    windows = (
        (
            NewQuotaWindow(
                bucket_key="300",
                window_kind="five_hour",
                kind_inferred=False,
                used_percent=98.0,
                resets_at=2000,
                window_minutes=300,
                is_exhausted=False,
            ),
            NewQuotaWindow(
                bucket_key="10080",
                window_kind="weekly",
                kind_inferred=False,
                used_percent=41.0,
                resets_at=3000,
                window_minutes=10080,
                is_exhausted=False,
            ),
        )
        if ok
        else ()
    )
    return NewQuotaSnapshot(
        source=source,
        captured_at=captured_at,
        ok=ok,
        error=None if ok else "boom",
        raw=raw,
        parser_version="quota/1",
        windows=windows,
    )


@pytest.fixture
def account_id(store, fake_clock):
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO accounts(codex_home, label, created_at) VALUES (?, ?, ?)",
            ("/tmp/codex-home", "main", fake_clock.epoch()),
        )
    return int(store.query_one("SELECT id FROM accounts")["id"])


@pytest.fixture
def dao(store):
    return QuotaSnapshotDao(store)


class TestInsertAndQuery:
    def test_roundtrip_with_windows(self, dao, account_id, fake_clock):
        snapshot_id = dao.insert(account_id, make_snapshot(), ingested_at=fake_clock.epoch())
        assert snapshot_id is not None
        row = dao.latest_ok(account_id)
        assert row is not None
        assert row["source"] == "appserver" and row["captured_at"] == 1000
        assert row["ok"] is True and row["parser_version"] == "quota/1"
        assert [(w["bucket_key"], w["window_kind"], w["used_percent"]) for w in row["windows"]] == [
            ("300", "five_hour", 98.0),
            ("10080", "weekly", 41.0),
        ]

    def test_latest_ok_orders_by_captured_at(self, dao, account_id, fake_clock):
        dao.insert(account_id, make_snapshot(captured_at=1000), ingested_at=fake_clock.epoch())
        dao.insert(
            account_id,
            make_snapshot(source="jsonl", captured_at=2000),
            ingested_at=fake_clock.epoch(),
        )
        row = dao.latest_ok(account_id)
        assert row["captured_at"] == 2000 and row["source"] == "jsonl"

    def test_latest_ok_source_filter(self, dao, account_id, fake_clock):
        dao.insert(account_id, make_snapshot(captured_at=1000), ingested_at=fake_clock.epoch())
        dao.insert(
            account_id,
            make_snapshot(source="jsonl", captured_at=2000),
            ingested_at=fake_clock.epoch(),
        )
        row = dao.latest_ok(account_id, source="appserver")
        assert row["captured_at"] == 1000
        assert dao.latest_ok(account_id, source="appserver")["source"] == "appserver"

    def test_error_snapshots_excluded_from_latest_ok(self, dao, account_id, fake_clock):
        dao.insert(account_id, make_snapshot(captured_at=1000), ingested_at=fake_clock.epoch())
        dao.insert(
            account_id,
            make_snapshot(captured_at=5000, ok=False),
            ingested_at=fake_clock.epoch(),
        )
        assert dao.latest_ok(account_id)["captured_at"] == 1000
        error = dao.latest_error(account_id)
        assert error is not None and error["captured_at"] == 5000 and error["error"] == "boom"

    def test_no_snapshots_returns_none(self, dao, account_id):
        assert dao.latest_ok(account_id) is None
        assert dao.latest_error(account_id) is None


class TestDedupAndLimits:
    def test_same_source_and_captured_at_deduped(self, dao, account_id, fake_clock):
        """persistence §8:同账户同源同 captured_at 去重不入库。"""

        first = dao.insert(
            account_id, make_snapshot(captured_at=1000), ingested_at=fake_clock.epoch()
        )
        second = dao.insert(
            account_id, make_snapshot(captured_at=1000), ingested_at=fake_clock.epoch() + 60
        )
        assert first is not None and second is None
        count = dao._store.query_one(
            "SELECT COUNT(*) AS n FROM quota_snapshots WHERE account_id = ?", (account_id,)
        )["n"]
        assert count == 1

    def test_different_captured_at_kept(self, dao, account_id, fake_clock):
        dao.insert(account_id, make_snapshot(captured_at=1000), ingested_at=fake_clock.epoch())
        assert (
            dao.insert(account_id, make_snapshot(captured_at=1001), ingested_at=fake_clock.epoch())
            is not None
        )

    def test_raw_truncated_to_64kib(self, dao, account_id, fake_clock):
        raw = '{"pad": "' + "x" * (80 * 1024) + '"}'
        dao.insert(account_id, make_snapshot(raw=raw), ingested_at=fake_clock.epoch())
        stored = dao._store.query_one("SELECT raw FROM quota_snapshots")["raw"]
        assert stored is not None and len(stored.encode("utf-8")) <= 64 * 1024

    def test_foreign_account_isolated(self, dao, store, account_id, fake_clock):
        """S-09/T-QUOTA-08(存储侧):快照按账户隔离。"""

        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO accounts(codex_home, created_at) VALUES (?, ?)",
                ("/tmp/other-home", fake_clock.epoch()),
            )
        other = int(
            store.query_one("SELECT id FROM accounts WHERE codex_home = '/tmp/other-home'")["id"]
        )
        dao.insert(account_id, make_snapshot(captured_at=1000), ingested_at=fake_clock.epoch())
        assert dao.latest_ok(other) is None

    def test_unknown_account_fk_rejected(self, dao, fake_clock):
        with pytest.raises(sqlite3.IntegrityError):
            dao.insert(999999, make_snapshot(), ingested_at=fake_clock.epoch())
