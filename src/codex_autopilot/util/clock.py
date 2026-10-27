"""可注入时间源(TimeSource)。

全部领域代码依赖本模块的 `TimeSource` 协议而非直接调用 `time`/`datetime`,
使超时、节奏与新鲜度判定可确定性测试(NFR-04/NFR-05,testing.md §2 假时钟)。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class TimeSource(Protocol):
    """时间源端口:领域代码只允许通过此协议取时间。"""

    def now(self) -> datetime:
        """当前时刻,必须为 UTC 时区感知(tz-aware)。"""

    def epoch(self) -> int:
        """当前 Unix 秒(UTC)。持久化层的时间戳统一用此值(D-PER-6)。"""

    def monotonic(self) -> float:
        """单调时钟,用于间隔/超时测量,不受系统时间跳变影响。"""


class SystemClock:
    """生产实现:系统墙钟 + 单调钟。"""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def epoch(self) -> int:
        return int(time.time())

    def monotonic(self) -> float:
        return time.monotonic()
