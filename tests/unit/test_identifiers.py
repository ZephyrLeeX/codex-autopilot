"""P2-T3 标识符校验测试:UUID canonical 化、会话名规则(FR-01-A1、§2.3)。"""

from __future__ import annotations

import pytest

from codex_autopilot.util.identifiers import (
    InvalidIdentifierError,
    canonical_uuid,
    is_canonical_uuid,
    validate_session_name,
)

GOOD = "01234567-89ab-cdef-0123-456789abcdef"


class TestCanonicalUuid:
    def test_accepts_canonical(self):
        assert canonical_uuid(GOOD) == GOOD

    def test_uppercase_normalized_to_lower(self):
        assert canonical_uuid(GOOD.upper()) == GOOD

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "not-a-uuid",
            "01234567-89ab-cdef-0123-456789abcde",  # 少一位
            "0123456789abcdef0123456789abcdef",  # 无连字符
            "{01234567-89ab-cdef-0123-456789abcdef}",
            "urn:uuid:01234567-89ab-cdef-0123-456789abcdef",
            "01234567-89ab-cdef-0123-456789abcdeg",  # 非十六进制
            "  01234567-89ab-cdef-0123-456789abcdef",  # 前后空白
            None,
            123,
        ],
    )
    def test_rejects_non_canonical(self, value):
        assert not is_canonical_uuid(value) if isinstance(value, str) else True
        with pytest.raises(InvalidIdentifierError):
            canonical_uuid(value)


class TestSessionName:
    @pytest.mark.parametrize(
        "name",
        ["a", "abc", "a1", "a-b-c", "session-01", "x" * 64],
    )
    def test_accepts_valid(self, name):
        assert validate_session_name(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "Abc",  # 大写
            "1abc",  # 数字开头
            "-abc",  # 连字符开头
            "abc_",  # 下划线
            "abc x",  # 空格
            "x" * 65,  # 超长
            None,
            42,
        ],
    )
    def test_rejects_invalid(self, name):
        with pytest.raises(InvalidIdentifierError):
            validate_session_name(name)
