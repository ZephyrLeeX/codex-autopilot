"""P2 集成:并发写与锁行为(T-STORE 并发、S-17)。

- WAL + busy_timeout:两个连接(模拟守护进程 + CLI)一读一写互不阻塞,
  写写冲突由 busy_timeout 串行化(FR-09-A2);
- 超过 busy_timeout 的长事务 → OperationalError(调用方决定重试/报错);
- sqlite3 连接不跨线程(check_same_thread 默认开启,persistence §2);
  每个线程各自打开自己的 Store,模拟独立进程。

单实例锁跨进程语义见 unit/test_lock.py。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from codex_autopilot.store import Store


class _LockHolder(threading.Thread):
    """在工作线程内打开自己的连接并持写锁,模拟"另一个进程"。"""

    def __init__(self, db: Path, fake_clock, hold_seconds: float | None = None):
        super().__init__(daemon=True)
        self.db = db
        self.clock = fake_clock
        self.hold_seconds = hold_seconds
        self.started = threading.Event()
        self.release = threading.Event()
        self.failure: BaseException | None = None

    def run(self) -> None:
        try:
            store = Store.open(self.db, clock=self.clock)
            try:
                with store.transaction() as conn:
                    conn.execute(
                        "INSERT INTO runtime_flags(key, value, updated_at) VALUES ('a', '1', 1)"
                    )
                    self.started.set()
                    if self.hold_seconds is not None:
                        time.sleep(self.hold_seconds)
                    else:
                        assert self.release.wait(timeout=5)
            finally:
                store.close()
        except BaseException as exc:  # 线程诊断:把失败带回主线程断言
            self.failure = exc
            self.started.set()


class TestConcurrentWriters:
    def test_busy_timeout_serializes_writes(self, tmp_path: Path, fake_clock):
        db = tmp_path / "state.db"
        Store.open(db, clock=fake_clock).close()  # 先建库,避免两线程迁移竞争
        holder = _LockHolder(db, fake_clock, hold_seconds=0.5)
        holder.start()
        assert holder.started.wait(timeout=5)
        contender = Store.open(db, clock=fake_clock)
        try:
            begin = time.monotonic()
            # busy_timeout=5000 → 等对方提交后成功,两笔写入都不丢(FR-09-A2)
            with contender.transaction() as conn:
                conn.execute(
                    "INSERT INTO runtime_flags(key, value, updated_at) VALUES ('b', '2', 2)"
                )
            assert time.monotonic() - begin >= 0.4
        finally:
            holder.join(timeout=5)
            contender.close()
        assert holder.failure is None
        rows = Store.open(db, clock=fake_clock)
        try:
            assert [
                row["key"] for row in rows.query("SELECT key FROM runtime_flags ORDER BY key")
            ] == ["a", "b"]
        finally:
            rows.close()

    def test_busy_timeout_expiry_raises(self, tmp_path: Path, fake_clock):
        db = tmp_path / "state.db"
        Store.open(db, clock=fake_clock).close()
        holder = _LockHolder(db, fake_clock, hold_seconds=1.0)
        holder.start()
        assert holder.started.wait(timeout=5)
        contender = Store.open(db, clock=fake_clock)
        contender.execute("PRAGMA busy_timeout=150")  # 模拟急性子调用方
        try:
            with (
                pytest.raises(sqlite3.OperationalError, match="locked"),
                contender.transaction() as conn,
            ):
                conn.execute(
                    "INSERT INTO runtime_flags(key, value, updated_at) VALUES ('b', '2', 2)"
                )
        finally:
            holder.join(timeout=5)
            contender.close()
        assert holder.failure is None

    def test_reader_not_blocked_by_writer(self, tmp_path: Path, fake_clock):
        """WAL 语义:写事务进行中,另一连接的读照常进行。"""

        db = tmp_path / "state.db"
        seed = Store.open(db, clock=fake_clock)
        seed.audit(actor="daemon", category="tick", level="info", summary="seed")
        seed.close()

        holder = _LockHolder(db, fake_clock)
        holder.start()
        assert holder.started.wait(timeout=5)
        reader = Store.open(db, clock=fake_clock)
        try:
            rows = reader.query("SELECT * FROM audit_log")  # 读不被阻塞
            assert len(rows) == 1
        finally:
            holder.release.set()
            holder.join(timeout=5)
            reader.close()
        assert holder.failure is None
