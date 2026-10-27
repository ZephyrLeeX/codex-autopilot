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
