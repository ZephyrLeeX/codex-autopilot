"""假时钟:可拨动、单调,替代 SystemClock 注入(testing.md §2、NFR-05)。

语义与 SystemClock 对齐:
- `now()` 返回 UTC 时区感知 datetime;`epoch()` 为对应 Unix 秒;
- `monotonic()` 只随 `advance()` 前进,模拟真实单调钟不受墙钟回拨影响
  (NFR-04:DST/回拨场景由测试显式构造)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class FakeClock:
    def __init__(self, start: float | datetime | None = None):
        if isinstance(start, datetime):
            self._now = start if start.tzinfo else start.replace(tzinfo=UTC)
        else:
            self._now = datetime.fromtimestamp(start if start is not None else 0, tz=UTC)
        self._monotonic = 1000.0  # 任意非零起点,避免与"未初始化"混淆

    def now(self) -> datetime:
        return self._now

    def epoch(self) -> int:
        return int(self._now.timestamp())

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        """时间前进(负值允许,模拟墙钟回拨;monotonic 只前进)。"""

        self._now = self._now + timedelta(seconds=seconds)
        if seconds > 0:
            self._monotonic += seconds

    def set(self, moment: datetime) -> None:
        self._now = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
