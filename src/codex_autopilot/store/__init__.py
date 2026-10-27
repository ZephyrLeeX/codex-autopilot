"""Store:SQLite 连接、PRAGMA、事务与迁移入口(persistence.md §1-§2,P2-T2)。

持久化安全性决策(Phase 2 复审结论,详见 persistence.md §2.1):
- `journal_mode=WAL`:读写不互斥,守护进程与 CLI 可并存;
- `synchronous=FULL`(**不是** NORMAL):每次 COMMIT 同步 fsync WAL。
  若用 NORMAL,投递协议(persistence §5)中"attempting 先落库 → 再 spawn
  `codex queue`"的次序保证会被打破——attempting 提交可能尚未 fsync 就发生
  spawn,掉电后该行丢失而队列消息已发出,守门重读看不到任何投递记录,
  将导致同一失败二次投递,违反 FR-05-A5/FR-08-A7。同理,投递门控状态
  (global_pause、enabled 等)丢写也会造成不该发生的投递。写入量极小
  (每 tick 数条),FULL 的代价可忽略。该值不暴露为配置项(配置不可弱化
  安全,FR-07-A5 同理)。

事务纪律(persistence §2):所有多语句写一律 `transaction()`
(BEGIN IMMEDIATE → … → COMMIT,异常 ROLLBACK);禁止隐式事务与嵌套事务。
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Self

from codex_autopilot.store import migrations as migrations_mod
from codex_autopilot.store.errors import (
    NestedTransactionError,
    StoreError,
    StoreNotOpenError,
)
from codex_autopilot.util.audit import write_audit
from codex_autopilot.util.clock import SystemClock, TimeSource

BUSY_TIMEOUT_MS = 5000

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Store:
    """单连接 SQLite 存储;每进程一个实例(守护进程/CLI 各自)。"""

    def __init__(self, conn: sqlite3.Connection, path: Path, clock: TimeSource):
        self._conn = conn
        self.path = path
        self._clock = clock
        self._in_tx = False
        self._closed = False

    # -- 生命周期 ---------------------------------------------------------

    @classmethod
    def open(cls, path: Path, *, clock: TimeSource | None = None) -> Store:
        """打开(必要时创建)数据库并应用迁移。

        抛 StoreError:目录不可创建、PRAGMA 不生效、迁移失败、版本不一致。
        """

        clock = clock or SystemClock()
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StoreError(f"cannot create state dir {path.parent}: {exc}") from exc
        try:
            conn = sqlite3.connect(
                path,
                isolation_level=None,  # 显式事务;禁止隐式提交(persistence §2)
                timeout=BUSY_TIMEOUT_MS / 1000,
            )
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open database {path}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        store = cls(conn, path, clock)
        try:
            store._apply_pragmas()
            try:
                migrations_mod.apply_migrations(
                    conn,
                    migrations_mod.discover_migrations(_MIGRATIONS_DIR),
                    now=clock.epoch(),
                )
            except migrations_mod.MigrationError as exc:
                raise StoreError(f"database migration failed: {exc}") from exc
        except Exception:
            conn.close()
            raise
        return store

    def _apply_pragmas(self) -> None:
        try:
            mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise StoreError(
                    f"journal_mode=WAL not active (got {mode!r}); "
                    "WAL is required for daemon/CLI concurrency (persistence.md §2)"
                )
            self._conn.execute("PRAGMA synchronous=FULL")
            sync = int(self._conn.execute("PRAGMA synchronous").fetchone()[0])
            if sync != 2:  # 2 = FULL
                raise StoreError(f"synchronous=FULL not active (got {sync})")
            self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            self._conn.execute("PRAGMA foreign_keys=ON")
            if int(self._conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise StoreError("foreign_keys=ON not active")
        except sqlite3.Error as exc:
            raise StoreError(f"failed to apply connection pragmas: {exc}") from exc

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def in_transaction(self) -> bool:
        return self._in_tx

    def _check_open(self) -> sqlite3.Connection:
        if self._closed:
            raise StoreNotOpenError("store is closed")
        return self._conn

    # -- 事务 -------------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务:BEGIN IMMEDIATE → 调用方语句 → COMMIT;异常 ROLLBACK。"""

        conn = self._check_open()
        if self._in_tx:
            raise NestedTransactionError("nested transaction attempted")
        conn.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield conn
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        finally:
            self._in_tx = False

    # -- 原子读(无需事务) ----------------------------------------------

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._check_open().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._check_open().execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """单语句写(自动提交);多语句写必须走 transaction()。"""

        return self._check_open().execute(sql, params)

    # -- 审计(append-only;util.audit 是唯一写入路径) --------------------

    def audit(
        self,
        *,
        actor: str,
        category: str,
        level: str,
        summary: str,
        session_uuid: str | None = None,
        data: dict | None = None,
        ts: int | None = None,
    ) -> int:
        """插入审计记录。独立小事务(避免调用方忘记提交即丢失审计)。"""

        with self.transaction() as conn:
            return write_audit(
                conn,
                ts=ts if ts is not None else self._clock.epoch(),
                actor=actor,
                category=category,
                level=level,
                summary=summary,
                session_uuid=session_uuid,
                data=data,
            )

    # -- 维护辅助(守护进程启动清扫/doctor 用,Phase 4+ 消费) -----------

    def integrity_ok(self) -> bool:
        row = self._check_open().execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"

    def checkpoint_wal(self) -> None:
        self._check_open().execute("PRAGMA wal_checkpoint(TRUNCATE)")

    @property
    def clock(self) -> TimeSource:
        return self._clock
