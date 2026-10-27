"""P3-T3 集成测试:App Server 只读客户端 x Fake app-server(L1,无真实 codex)。

覆盖:握手/查询/超时/错误响应/服务端反向请求拒绝/通知记录/启动失败
(T-GUARD-06:日志断言仅白名单只读方法被调用)。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from codex_autopilot.codexif.appserver import (
    AppServerClient,
    AppServerError,
    AppServerLaunchError,
    AppServerTimeoutError,
)
from tests.conftest import write_scenario

FAST = {"call_timeout_sec": 2.0, "shutdown_grace_sec": 0.5}


def make_client(fake_appserver_env, tmp_path, **kwargs) -> AppServerClient:
    binary, _, _ = fake_appserver_env
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(parents=True, exist_ok=True)
    options = {**FAST, **kwargs}
    return AppServerClient(
        binary=str(binary), codex_home=codex_home, client_version="0.0.0-test", **options
    )


def read_log(log_path: Path) -> list[dict]:
    entries = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def received_methods(log_path: Path) -> list[str]:
    return [
        entry["msg"]["method"]
        for entry in read_log(log_path)
        if entry["dir"] == "recv"
        and isinstance(entry.get("msg"), dict)
        and "method" in entry["msg"]
    ]


class TestHandshakeAndQuery:
    def test_initialize_then_rate_limits(self, fake_appserver_env, tmp_path):
        _, scenario, _ = fake_appserver_env
        write_scenario(scenario)
        client = make_client(fake_appserver_env, tmp_path)
        with client:
            result = client.read_rate_limits()
        assert result["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] == 41
        assert client.user_agent == "codex-cli fake (testing)"

    def test_only_whitelisted_methods_called(self, fake_appserver_env, tmp_path):
        """T-GUARD-06:Fake 记录全部收到的方法,仅白名单只读方法出现。"""

        _, scenario, log = fake_appserver_env
        write_scenario(scenario)
        with make_client(fake_appserver_env, tmp_path) as client:
            client.read_rate_limits()
        methods = received_methods(log)
        assert set(methods) <= {"initialize", "initialized", "account/rateLimits/read"}

    def test_initialized_notification_sent_after_result(self, fake_appserver_env, tmp_path):
        _, scenario, log = fake_appserver_env
        write_scenario(scenario)
        with make_client(fake_appserver_env, tmp_path):
            pass
        entries = read_log(log)
        recv = [e["msg"] for e in entries if e["dir"] == "recv" and isinstance(e["msg"], dict)]
        assert {"method": "initialized"} in recv

    def test_codex_home_env_passed(self, fake_appserver_env, tmp_path):
        _, scenario, log = fake_appserver_env
        write_scenario(scenario)
        codex_home = tmp_path / "codex-home"
        with make_client(fake_appserver_env, tmp_path):
            pass
        env_entries = [e["msg"] for e in read_log(log) if e["dir"] == "env"]
        assert env_entries and env_entries[0]["CODEX_HOME"] == str(codex_home)

    def test_custom_scenario_result_roundtrip(self, fake_appserver_env, tmp_path):
        _, scenario, _ = fake_appserver_env
        write_scenario(scenario, rate_limits_result={"rateLimits": {"primary": {"usedPercent": 7}}})
        with make_client(fake_appserver_env, tmp_path) as client:
            assert client.read_rate_limits() == {"rateLimits": {"primary": {"usedPercent": 7}}}


class TestTimeoutsAndErrors:
    def test_hang_on_rate_limits_times_out(self, fake_appserver_env, tmp_path):
        _, scenario, _ = fake_appserver_env
        write_scenario(scenario, hang_rate_limits=True)
        client = make_client(fake_appserver_env, tmp_path, call_timeout_sec=0.8)
        with pytest.raises(AppServerTimeoutError), client:
            client.read_rate_limits()
        # 连接废弃后再调用立即失败(不再等待)
        with pytest.raises(AppServerError, match="unusable"):
            client.read_rate_limits()

    def test_error_response_raises(self, fake_appserver_env, tmp_path):
        _, scenario, _ = fake_appserver_env
        write_scenario(
            scenario,
            rate_limits_error={"code": -32000, "message": "backend unavailable"},
        )
        with (
            pytest.raises(AppServerError, match="-32000"),
            make_client(fake_appserver_env, tmp_path) as client,
        ):
            client.read_rate_limits()

    def test_garbage_line_protocol_error(self, fake_appserver_env, tmp_path):
        _, scenario, _ = fake_appserver_env
        write_scenario(scenario, garbage_before_rate_limits=True)
        with (
            pytest.raises(AppServerError, match="non-JSON"),
            make_client(fake_appserver_env, tmp_path) as client,
        ):
            client.read_rate_limits()

    def test_server_exit_mid_call_raises(self, fake_appserver_env, tmp_path):
        _, scenario, _ = fake_appserver_env
        write_scenario(scenario, exit_before_handshake=True, exit_code=3)
        client = make_client(fake_appserver_env, tmp_path)
        with pytest.raises(AppServerError, match="exited"), client:
            pass  # __enter__ 内握手失败,含 stderr 尾部诊断

    def test_missing_binary_is_launch_error(self, tmp_path):
        client = AppServerClient(
            binary=str(tmp_path / "no-such-binary"),
            codex_home=tmp_path,
            client_version="0",
            call_timeout_sec=1.0,
            shutdown_grace_sec=0.5,
        )
        with pytest.raises(AppServerLaunchError):
            client.start()


class TestServerSideMessages:
    def test_server_request_rejected_with_32601(self, fake_appserver_env, tmp_path):
        """服务端反向请求一律 -32601 拒绝,不阻塞原查询。"""

        _, scenario, log = fake_appserver_env
        write_scenario(
            scenario,
            server_request_before_rate_limits={
                "id": 9001,
                "method": "fs/writeFile",
                "params": {"path": "/etc/passwd"},
            },
        )
        with make_client(fake_appserver_env, tmp_path) as client:
            result = client.read_rate_limits()  # 查询不受影响
        assert result
        assert client.rejected_server_requests == ["fs/writeFile"]
        replies = [
            entry["msg"]
            for entry in read_log(log)
            if entry["dir"] == "recv" and isinstance(entry["msg"], dict) and "error" in entry["msg"]
        ]
        assert replies and replies[0]["id"] == 9001
        assert replies[0]["error"]["code"] == -32601

    def test_notification_recorded_not_fatal(self, fake_appserver_env, tmp_path):
        _, scenario, _ = fake_appserver_env
        write_scenario(
            scenario,
            push_notification_before_rate_limits={
                "method": "account/rateLimits/updated",
                "params": {"reason": "test"},
            },
        )
        with make_client(fake_appserver_env, tmp_path) as client:
            assert client.read_rate_limits()
        assert [n["method"] for n in client.notifications] == ["account/rateLimits/updated"]

    def test_read_before_start_rejected(self, tmp_path):
        client = AppServerClient(
            binary="codex",
            codex_home=tmp_path,
            client_version="0",
            call_timeout_sec=1.0,
        )
        with pytest.raises(AppServerError, match="not started"):
            client.read_rate_limits()


class TestLifecycle:
    def test_stop_terminates_hung_process_quickly(self, fake_appserver_env, tmp_path):
        _, scenario, _ = fake_appserver_env
        write_scenario(scenario, hang_initialize=True)
        client = make_client(
            fake_appserver_env, tmp_path, call_timeout_sec=0.6, shutdown_grace_sec=0.5
        )
        started = time.monotonic()
        try:
            with pytest.raises(AppServerTimeoutError):
                client.start()  # 直接调用 start(非 with),失败路径也要能回收
        finally:
            client.stop()
        assert time.monotonic() - started < 5  # 兜底:不留 3600s 挂起进程

    def test_double_start_rejected(self, fake_appserver_env, tmp_path):
        _, scenario, _ = fake_appserver_env
        write_scenario(scenario)
        client = make_client(fake_appserver_env, tmp_path)
        client.start()
        try:
            with pytest.raises(AppServerError, match="already started"):
                client.start()
        finally:
            client.stop()
