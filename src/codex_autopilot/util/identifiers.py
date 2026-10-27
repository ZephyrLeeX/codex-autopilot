"""标识符校验:会话 UUID 与会话名(session-management.md §2)。

UUID 规则:canonical 8-4-4-4-12 十六进制;接受大写输入但一律规范化为小写存储。
名称规则:`^[a-z][a-z0-9-]{0,63}$`(小写字母开头,数字与连字符,≤64 字符)。
"""

from __future__ import annotations

import re

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

SESSION_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class InvalidIdentifierError(ValueError):
    """UUID 或名称不符合规范。"""


def canonical_uuid(value: str) -> str:
    """校验并返回 canonical 小写 UUID;不合法抛 InvalidIdentifierError。

    刻意用正则而非 `uuid.UUID(...)`:uuid.UUID 接受 URN、花括号等非 canonical
    写法,而注册主键必须收紧到唯一一种存储形态(FR-01-A1)。
    """

    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise InvalidIdentifierError(
            f"invalid session UUID {value!r}: expected canonical 8-4-4-4-12 hex form "
            "(e.g. 01234567-89ab-cdef-0123-456789abcdef)"
        )
    return value.lower()


def is_canonical_uuid(value: str) -> bool:
    try:
        canonical_uuid(value)
    except InvalidIdentifierError:
        return False
    return True


def validate_session_name(value: str) -> str:
    """校验会话名并原样返回;不合法抛 InvalidIdentifierError(session-management §2.3)。"""

    if not isinstance(value, str) or not SESSION_NAME_RE.fullmatch(value):
        raise InvalidIdentifierError(
            f"invalid session name {value!r}: must match ^[a-z][a-z0-9-]{{0,63}}$ "
            "(lowercase letter first, then digits/hyphens, max 64 chars)"
        )
    return value
