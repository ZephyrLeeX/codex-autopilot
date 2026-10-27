"""骨架冒烟测试:验证包可导入、CLI 入口可加载。

Phase 2 起本文件将被真实测试替代/扩充。运行:pytest tests/unit/test_skeleton.py
"""

import codex_autopilot
from codex_autopilot import cli


def test_package_version() -> None:
    assert isinstance(codex_autopilot.__version__, str)
    assert codex_autopilot.__version__.startswith("0.")


def test_cli_group_loads() -> None:
    assert callable(cli.main)
