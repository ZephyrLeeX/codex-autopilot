"""共享 fixtures:隔离的 HOME/XDG 环境、假时钟、CliRunner、Store。

L1 红线(testing.md §1):测试绝不触碰开发者真实的 `~/.codex`;
autouse 的 `isolated_home` 把 HOME 与 XDG 变量全部指到 tmp_path,并清除
环境里泄漏的 `CODEX_AUTOPILOT_*` 覆盖。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from codex_autopilot.store import Store
from tests.fakes.clock import FakeClock


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    for key in list(os.environ):
        if key.startswith("CODEX_AUTOPILOT_"):
            monkeypatch.delenv(key, raising=False)
    return home


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(start=1767225600)  # 2026-01-01T00:00:00Z,确定性起点


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cli(runner: CliRunner):
    """`cli(*args)` → CliResult;默认走隔离环境(继承 monkeypatch 后的 os.environ)。"""

    from codex_autopilot.cli.main import main as cli_main

    def invoke(*args: str, **kwargs):
        return runner.invoke(cli_main, list(args), **kwargs)

    return invoke


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    directory = tmp_path / "codex-home"
    directory.mkdir()
    return directory


@pytest.fixture
def store(tmp_path: Path, fake_clock: FakeClock):
    store = Store.open(tmp_path / "state" / "state.db", clock=fake_clock)
    yield store
    store.close()


FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixture_path():
    """fixture 文件定位:fixture_path("jsonl", "torn_tail.jsonl") → Path。"""

    def _resolve(*parts: str) -> Path:
        return FIXTURES.joinpath(*parts)

    return _resolve


@pytest.fixture
def write_rollout(tmp_path: Path):
    """在隔离的 Codex Home 中构造 rollout 文件(保持内容原字节,撕裂尾行原样)。"""

    def _write(
        codex_home: Path,
        session_uuid: str,
        content: str,
        *,
        ts: str = "2026-01-01T00-00-00",
        suffix_uuid: str | None = None,
        archived: bool = False,
        subpath: str = "2026/01/01",
    ) -> Path:
        chain = session_uuid if suffix_uuid is None else f"{session_uuid}_{suffix_uuid}"
        name = f"rollout-{ts}-{chain}.jsonl"
        root = "archived_sessions" if archived else "sessions"
        directory = codex_home / root / subpath
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(content, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def fake_appserver_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """指向 Fake app-server 二进制的环境(L1 红线:绝不触真实 codex)。

    返回 (binary_path, scenario_path, log_path);写好 scenario 后调用即可。
    """

    binary = Path(__file__).resolve().parent / "fakes" / "app_server.py"
    scenario = tmp_path / "fake-appserver-scenario.json"
    log = tmp_path / "fake-appserver-log.jsonl"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("FAKE_APP_SERVER_SCRIPT", str(scenario))
    monkeypatch.setenv("FAKE_APP_SERVER_LOG", str(log))
    return binary, scenario, log


def write_scenario(scenario_path: Path, **settings) -> None:
    import json

    defaults = {
        "initialize_result": {
            "userAgent": "codex-cli fake (testing)",
            "platformFamily": "unix",
            "platformOS": "linux",
        },
        "rate_limits_result": {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "usedPercent": 41,
                        "resetsAt": 1792583368,
                        "windowDurationMins": 300,
                    },
                    "secondary": {
                        "usedPercent": 12,
                        "resetsAt": 1793099965,
                        "windowDurationMins": 10080,
                    },
                }
            },
            "ordinaryUsageAllowed": True,
        },
    }
    defaults.update(settings)
    scenario_path.write_text(json.dumps(defaults), encoding="utf-8")
