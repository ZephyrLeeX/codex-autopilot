"""按账户常驻的 App Server 连接池(quota-monitoring §4/D-QM-5;P4-T5)。

- 每账户至多一个子进程,惰性启动、跨 tick 复用(免每 tick 冷启动);
- 进程死亡/调用失败:丢弃连接,按退避重启(60→120→300s 封顶,§6);
- 空闲超时回收(默认 600s,可配);
- `lease()` 返回上下文管理器:退出时**不关闭**健康连接(归还池),
  与 QuotaService 的一次性 `with client:` 用法兼容(同一工厂签名)。

L1 测试经 Fake app-server(剧本驱动)覆盖;真实二进制上的常驻稳定性
留待 L2/L3(quota-monitoring §9 未验证条目不变)。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from codex_autopilot.codexif.appserver import AppServerClient, AppServerError
from codex_autopilot.util.clock import TimeSource

logger = logging.getLogger(__name__)

BACKOFF_STEPS_SEC = (60, 120, 300)
DEFAULT_IDLE_TIMEOUT_SEC = 600.0


class _Lease:
    """池化连接的包装:实现与 AppServerClient 相同的上下文协议。

    `with lease:` 期间连接存活;退出时健康连接归还池,死亡连接丢弃。
    属性访问透传内部客户端(read_rate_limits 等)。
    """

    def __init__(self, client: AppServerClient, pool: _AccountPool):
        self._client = client
        self._pool = pool

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def __enter__(self) -> _Lease:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._pool.release(self._client)


class _AccountPool:
    def __init__(
        self,
        *,
        account_id: int,
        binary: str,
        codex_home: Path,
        client_version: str,
        clock: TimeSource,
        idle_timeout_sec: float,
    ):
        self.account_id = account_id
        self._binary = binary
        self._codex_home = codex_home
        self._client_version = client_version
        self._clock = clock
        self._idle_timeout = idle_timeout_sec
        self._client: AppServerClient | None = None
        self._last_used = 0.0
        self._failures = 0

    def _backoff_remaining(self) -> float:
        if self._failures == 0:
            return 0.0
        step = BACKOFF_STEPS_SEC[min(self._failures - 1, len(BACKOFF_STEPS_SEC) - 1)]
        return max(0.0, step - (self._clock.epoch() - self._last_used))

    def acquire(self) -> _Lease:
        remaining = self._backoff_remaining()
        if remaining > 0:
            raise AppServerError(
                f"app-server pool for account {self.account_id} in backoff"
                f" ({remaining:.0f}s remaining)"
            )
        client = self._client
        if client is not None and not client.usable:
            self._stop_client(client)
            self._client = None
        if self._client is None:
            client = AppServerClient(
                binary=self._binary,
                codex_home=self._codex_home,
                client_version=self._client_version,
            )
            client.start()  # 握手失败 → AppServerLaunchError → 调用方记错误快照
            self._client = client
            self._failures = 0
        return _Lease(self._client, self)

    def release(self, client: AppServerClient) -> None:
        self._last_used = float(self._clock.epoch())
        if client is not self._client:
            self._stop_client(client)  # 陈旧实例(池已换新):直接回收
            return
        if not client.usable:
            self._failures += 1
            self._stop_client(client)
            self._client = None

    def recycle_if_idle(self, *, now_epoch: int) -> bool:
        """空闲超时回收;返回是否回收了连接。"""

        if self._client is None:
            return False
        if now_epoch - self._last_used < self._idle_timeout:
            return False
        self._stop_client(self._client)
        self._client = None
        return True

    def stop(self) -> None:
        if self._client is not None:
            self._stop_client(self._client)
            self._client = None

    def _stop_client(self, client: AppServerClient) -> None:
        try:
            client.stop()
        except Exception:
            logger.warning("app-server pool: client stop failed", exc_info=True)


class AppServerPool:
    """全部受管账户的连接池(调度器持有;单线程使用)。"""

    def __init__(
        self,
        *,
        clock: TimeSource,
        client_version: str,
        idle_timeout_sec: float = DEFAULT_IDLE_TIMEOUT_SEC,
    ):
        self._clock = clock
        self._client_version = client_version
        self._idle_timeout = idle_timeout_sec
        self._pools: dict[int, _AccountPool] = {}
        self._binary: str | None = None

    def set_binary(self, binary: str) -> None:
        self._binary = binary

    def lease(self, account_id: int, codex_home: Path) -> _Lease:
        """取该账户的连接(惰性启动)。异常语义与一次性客户端一致。"""

        if self._binary is None:
            raise AppServerError("pool binary not configured (set_binary first)")
        pool = self._pools.get(account_id)
        if pool is None:
            pool = _AccountPool(
                account_id=account_id,
                binary=self._binary,
                codex_home=Path(codex_home),
                client_version=self._client_version,
                clock=self._clock,
                idle_timeout_sec=self._idle_timeout,
            )
            self._pools[account_id] = pool
        return pool.acquire()

    def recycle_idle(self) -> int:
        """回收空闲超时连接(每 tick 末调用);返回回收数。"""

        now_epoch = self._clock.epoch()
        recycled = 0
        for pool in self._pools.values():
            if pool.recycle_if_idle(now_epoch=now_epoch):
                recycled += 1
        return recycled

    def stop_all(self) -> None:
        for pool in self._pools.values():
            pool.stop()
        self._pools.clear()
