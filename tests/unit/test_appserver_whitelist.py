"""T-GUARD-02/06(部分):App Server 客户端白名单静态锁定(FR-07-A1/A2)。

- 黑名单方法字符串在 src/ 零出现(queue-safety §8);
- 请求构造仅存在于具名白名单方法内,无通用 call() 逃生门;
- 白名单恰好等于 {initialize, account/rateLimits/read}。
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

# queue-safety.md §8 黑名单(出现即失败;src 范围)
BLACKLISTED_METHOD_PATTERNS = [
    "account/rateLimitResetCredit/consume",
    "account/sendAddCreditsNudgeEmail",
    "account/login",
    "account/logout",
    "account/gatewayOAuth",
    "marketplace/",
    "plugin/share",
    "thread/queue/delete",
    "thread/queue/add",
    "thread/start",
    "thread/delete",
    "thread/archive",
    "thread/inject_items",
    "turn/start",
    "turn/steer",
    "turn/interrupt",
    "config/batchWrite",
    "config/value/write",
    "fs/writeFile",
    "fs/remove",
    "fs/copy",
    "command/exec",
    "experimentalFeature/enablement/set",
    "skills/config/write",
]

EXPECTED_WHITELIST = {"initialize", "account/rateLimits/read"}

APPSERVER_SRC = (SRC / "codex_autopilot" / "codexif" / "appserver.py").read_text(encoding="utf-8")


def _sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


class TestBlacklistAbsent:
    def test_no_blacklisted_method_strings_in_src(self):
        for path in _sources():
            content = path.read_text(encoding="utf-8")
            for pattern in BLACKLISTED_METHOD_PATTERNS:
                assert pattern not in content, f"{path}: blacklisted method {pattern!r} found"


class TestWhitelistShape:
    def test_whitelist_exactly_two_readonly_methods(self):
        appserver = APPSERVER_SRC
        match = re.search(r"READ_METHODS = frozenset\(\{(.*?)\}\)", appserver, re.DOTALL)
        assert match is not None, "READ_METHODS frozenset not found"
        methods = set(re.findall(r'"([^"]+)"', match.group(1)))
        assert methods == EXPECTED_WHITELIST

    def test_method_constants_defined_as_whitelist(self):
        appserver = APPSERVER_SRC
        assert re.search(r'METHOD_INITIALIZE = "initialize"', appserver)
        assert re.search(r'METHOD_RATE_LIMITS = "account/rateLimits/read"', appserver)

    def test_request_construction_only_with_whitelisted_literals(self):
        """_send_request 调用只能传白名单成员或其具名常量(无任意字符串透传)。"""

        appserver = APPSERVER_SRC
        calls = re.findall(r"self\._send_request\(\s*([A-Za-z_][A-Za-z0-9_]*)", appserver)
        assert calls, "no _send_request calls found"
        allowed = EXPECTED_WHITELIST | {"METHOD_INITIALIZE", "METHOD_RATE_LIMITS"}
        for name in calls:
            assert name in allowed, f"_send_request called with non-whitelist {name}"

    def test_no_generic_call_escape_hatch(self):
        """AppServerClient 无接受任意方法名的公共调用面。"""

        appserver = APPSERVER_SRC
        assert not re.search(r"def call(?:_method)?\s*\(", appserver)
        # 命名空间方法字符串(account/*、thread/*)只允许出现在客户端模块
        string_methods = set(re.findall(r'"((?:account|thread)/[A-Za-z/]+)"', appserver))
        assert string_methods <= EXPECTED_WHITELIST

    def test_send_notification_only_initialized(self):
        appserver = APPSERVER_SRC
        notifications = re.findall(r"self\._send_notification\(\s*([A-Za-z0-9_\"]+)", appserver)
        assert notifications == ['"initialized"']

    def test_method_strings_confined_to_client_module(self):
        """app-server 方法名只允许出现在 codexif/appserver.py(领域/CLI 层零方法字符串)。"""

        for path in _sources():
            if path.name == "appserver.py" and path.parent.name == "codexif":
                continue
            content = path.read_text(encoding="utf-8")
            assert "account/rateLimit" not in content, f"{path}: app-server method leaked"
            assert "thread/queue/" not in content, f"{path}: app-server method leaked"
