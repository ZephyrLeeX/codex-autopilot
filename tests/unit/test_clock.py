"""P2-T3 时钟测试:T-CLK 基础(确定性、UTC、单调)。"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from codex_autopilot.util.clock import SystemClock, TimeSource
from tests.fakes.clock import FakeClock


class TestSystemClock:
    def test_now_is_tz_aware_utc(self):
        now = SystemClock().now()
        assert now.tzinfo is UTC

    def test_epoch_close_to_time_time(self):
        assert abs(SystemClock().epoch() - time.time()) < 5

    def test_monotonic_is_non_decreasing(self):
        clock = SystemClock()
        first = clock.monotonic()
        time.sleep(0.01)
        assert clock.monotonic() >= first


class TestFakeClock:
    def test_deterministic_epoch(self):
        clock = FakeClock(start=1767225600)
        assert clock.epoch() == 1767225600
        assert clock.now() == datetime(2026, 1, 1, tzinfo=UTC)

    def test_advance_moves_now_and_epoch(self):
        clock = FakeClock(start=1767225600)
        clock.advance(90)
        assert clock.epoch() == 1767225690
        assert clock.now() == datetime(2026, 1, 1, 0, 1, 30, tzinfo=UTC)

    def test_monotonic_tracks_advance_only(self):
        clock = FakeClock(start=1767225600)
        base = clock.monotonic()
        clock.advance(-3600)  # 墙钟回拨(模拟 NTP 修正)
        assert clock.epoch() == 1767225600 - 3600
        assert clock.monotonic() == base  # 单调钟不受影响(NFR-04)

    def test_satisfies_timesource_protocol(self):
        assert isinstance(FakeClock(), TimeSource)
        assert isinstance(SystemClock(), TimeSource)

    def test_set_with_naive_datetime_assumes_utc(self):
        clock = FakeClock(start=0)
        clock.set(datetime(2026, 3, 1, 12, 0, 0))
        assert clock.now().tzinfo is UTC

    def test_timedelta_advance_via_datetime(self):
        clock = FakeClock(start=datetime(2026, 1, 1, tzinfo=UTC))
        clock.advance(timedelta(minutes=5).total_seconds())
        assert clock.now().minute == 5
