"""源码静态扫描:红线不变量(T-GUARD-04 部分 + T-STORE-03 审计仅 INSERT)。

- NFR-03:不得硬编码 /home/ 路径、真实邮箱服务商域;
- persistence §4:audit_log 只允许 INSERT 代码路径;
- Phase 2 安全复审:synchronous=FULL 必须保留在源码中。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

SRC = Path(__file__).resolve().parents[2] / "src"


def _python_sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


class TestNoHardcodedUserArtifacts:
    def test_no_home_paths(self):
        for path in _python_sources():
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                assert "/home/" not in line, f"{path}:{lineno}: hardcoded /home/ path"

    def test_no_specific_mail_providers(self):
        # 任何具体邮箱服务商域都不得出现(examples/config.toml 的 example.com 除外,
        # 且该文件不在 src/ 内)
        for path in _python_sources():
            content = path.read_text()
            assert "@qq.com" not in content, f"{path}: hardcoded mail provider"
            assert "@163.com" not in content, f"{path}: hardcoded mail provider"

    # 允许的占位形状(明显合成,非真实会话):
    PLACEHOLDER_UUIDS: ClassVar[set[str]] = {"01234567-89ab-cdef-0123-456789abcdef"}

    def test_no_real_codex_session_uuids(self):
        """代码中出现的 UUID 字面量只能是占位形状(NFR-03)。"""

        pattern = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
        for path in _python_sources():
            for match in pattern.finditer(path.read_text()):
                value = match.group(0)
                assert value in self.PLACEHOLDER_UUIDS, (
                    f"{path}: UUID literal {value} is not a known placeholder shape; "
                    "real session IDs must never be hardcoded"
                )


class TestAuditInsertOnly:
    MUTATION_RE = re.compile(
        r"(UPDATE\s+audit_log|DELETE\s+FROM\s+audit_log|REPLACE\s+INTO\s+audit_log|"
        r"DROP\s+TABLE\s+audit_log|ALTER\s+TABLE\s+audit_log)",
        re.IGNORECASE,
    )

    def test_no_audit_mutation_statement_anywhere(self):
        """persistence §4:audit_log 无 UPDATE/DELETE/REPLACE/DROP/ALTER 语句。"""

        for path in _python_sources() + sorted(
            (SRC / "codex_autopilot" / "store" / "migrations").glob("*.sql")
        ):
            content = path.read_text()
            match = self.MUTATION_RE.search(content)
            assert match is None, f"{path}: mutating audit_log: {match and match.group(0)}"

    def test_audit_write_is_insert(self):
        audit_py = (SRC / "codex_autopilot" / "util" / "audit.py").read_text()
        assert "INSERT INTO audit_log" in audit_py


class TestPersistenceSafetyLiterals:
    def test_synchronous_full_pinned(self):
        """安全复审结论以源码字面量钉死:防止静默降回 NORMAL。"""

        store_init = (SRC / "codex_autopilot" / "store" / "__init__.py").read_text()
        assert "PRAGMA synchronous=FULL" in store_init
        assert "synchronous=NORMAL" not in store_init
