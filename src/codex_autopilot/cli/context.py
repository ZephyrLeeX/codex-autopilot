"""CLI 上下文与退出码契约(cli-design.md §6)。"""

from __future__ import annotations

import dataclasses

from codex_autopilot.config import Config
from codex_autopilot.store import Store
from codex_autopilot.util.clock import SystemClock, TimeSource

# 退出码(cli-design.md §6;脚本可依赖)
EXIT_OK = 0
EXIT_ERROR = 1  # 一般错误(如未找到会话、名称冲突)
EXIT_USAGE = 2  # 用法/参数校验错误(click 保留段)
EXIT_NEEDS_HUMAN = 10
EXIT_QUOTA_BLOCKED = 11
EXIT_UNKNOWN_DELIVERY = 12
EXIT_CONFIG_DB = 20  # 配置/数据库错误
EXIT_LOCK_HELD = 21  # 单实例锁被占(S-17)
EXIT_DOCTOR_FAIL = 30


class CliError(Exception):
    """命令级错误;`code` 进入 --json 错误体,`exit_code` 为进程退出码。"""

    def __init__(self, message: str, *, exit_code: int = EXIT_ERROR, code: str = "error"):
        super().__init__(message)
        self.exit_code = exit_code
        self.code = code


@dataclasses.dataclass
class CliContext:
    """贯穿一次 CLI 调用的状态:已解析配置 + 惰性 Store + 输出模式。"""

    config: Config
    json_output: bool
    clock: TimeSource
    _store: Store | None = dataclasses.field(default=None, repr=False)

    def store(self) -> Store:
        """惰性打开数据库(只读命令也走迁移,保证 schema 就绪)。"""

        if self._store is None or self._store.closed:
            self._store = Store.open(self.config.state_db, clock=self.clock)
        return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None


def make_context(
    *,
    config: Config,
    json_output: bool,
    clock: TimeSource | None = None,
) -> CliContext:
    return CliContext(
        config=config,
        json_output=json_output,
        clock=clock if clock is not None else SystemClock(),
    )
