"""P4-T5 App Server 连接池单元测试:复用、退避、空闲回收(Fake app-server)。"""

from __future__ import annotations

import contextlib
import json

from codex_autopilot.scheduling.appserver_pool import AppServerPool
from tests.conftest import write_scenario


def _log_entries(log) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _process_starts(log) -> int:
    return sum(1 for entry in _log_entries(log) if entry["dir"] == "env")


def _reads(log) -> int:
    return sum(
        1
        for entry in _log_entries(log)
        if entry["dir"] == "recv"
        and isinstance(entry["msg"], dict)
        and entry["msg"].get("method") == "account/rateLimits/read"
    )


class TestPoolReuse:
    def test_same_account_reuses_one_process(self, fake_appserver_env, fake_clock):
        binary, scenario, log = fake_appserver_env
        write_scenario(scenario)
        pool = AppServerPool(clock=fake_clock, client_version="0.0.0-test")
        pool.set_binary(str(binary))
        try:
            for _ in range(3):
                with pool.lease(1, fake_appserver_env[0].parent) as client:
                    result = client.read_rate_limits()
                    assert isinstance(result, dict)
            assert _process_starts(log) == 1  # 常驻复用,不重复 spawn
            assert _reads(log) == 3
        finally:
            pool.stop_all()

    def test_distinct_accounts_distinct_clients(self, fake_appserver_env, fake_clock):
        binary, scenario, log = fake_appserver_env
        write_scenario(scenario)
        pool = AppServerPool(clock=fake_clock, client_version="0.0.0-test")
        pool.set_binary(str(binary))
        try:
            with pool.lease(1, fake_appserver_env[0].parent):
                pass
            with pool.lease(2, fake_appserver_env[0].parent):
                pass
            assert _process_starts(log) == 2
        finally:
            pool.stop_all()

    def test_idle_recycle(self, fake_appserver_env, fake_clock):
        binary, scenario, log = fake_appserver_env
        write_scenario(scenario)
        pool = AppServerPool(clock=fake_clock, client_version="0.0.0-test", idle_timeout_sec=600)
        pool.set_binary(str(binary))
        try:
            with pool.lease(1, fake_appserver_env[0].parent):
                pass
            assert _process_starts(log) == 1
            assert pool.recycle_idle() == 0  # 未超时
            fake_clock.advance(601)
            assert pool.recycle_idle() == 1  # 超时回收
            with pool.lease(1, fake_appserver_env[0].parent):
                pass
            assert _process_starts(log) == 2  # 回收后重新启动
        finally:
            pool.stop_all()

    def test_backoff_after_failure(self, fake_appserver_env, fake_clock):
        """查询失败 → 连接判死 → 退避期内直接拒绝(不 spawn)。"""

        binary, scenario, _log = fake_appserver_env
        write_scenario(scenario, rate_limits_error={"code": -32000, "message": "boom"})
        pool = AppServerPool(clock=fake_clock, client_version="0.0.0-test")
        pool.set_binary(str(binary))
        try:
            lease = pool.lease(1, fake_appserver_env[0].parent)
            with contextlib.suppress(Exception):  # 错误响应 → AppServerError
                lease.read_rate_limits()
            lease.__exit__(None, None, None)  # 归还(判死)
            fake_clock.advance(30)  # 退避 60s 内
            try:
                pool.lease(1, fake_appserver_env[0].parent)
                raised = False
            except Exception:
                raised = True
            assert raised, "lease should be refused during backoff"
            fake_clock.advance(31)  # 越过退避窗口
            with pool.lease(1, fake_appserver_env[0].parent) as client:
                assert client.usable
        finally:
            pool.stop_all()
