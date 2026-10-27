#!/usr/bin/env python3
"""伪 codex app-server(testing.md §2,P3-T3 交付;L1 红线:绝不连真实账户)。

调用形态与真实二进制对齐:`<本脚本> app-server --stdio`(argv[0] 指向本文件,
见 testing.md §2"Fake codex CLI 通过 argv[0] 指向测试目录的脚本实现")。

剧本(scenario)与环境:
- `FAKE_APP_SERVER_SCRIPT`:JSON 文件路径,字段见 `_DEFAULT_SCENARIO`;
- `FAKE_APP_SERVER_LOG`:JSONL 日志文件路径,记录全部收发消息
  (T-GUARD-06:断言仅白名单只读方法被调用、反向请求被 -32601 拒绝);
- `CODEX_HOME`:接收并记录(断言账户隔离用),不访问。

行为开关(全部默认关闭):
- exit_before_handshake:启动即退出(测 LaunchError);
- hang_initialize / hang_rate_limits:挂起不回(测超时);
- rate_limits_error:以 JSON-RPC error 响应;
- garbage_before_rate_limits:先写一行非 JSON(测协议错误);
- push_notification_before_rate_limits:先推送服务端通知(E4);
- server_request_before_rate_limits:先发服务端反向请求(测 -32601 拒绝);
- reject_before_initialize:initialize 之前的请求回 -32002(真实服务端语义)。
"""

from __future__ import annotations

import json
import os
import sys
import time

ARGV_EXPECTED = ["app-server", "--stdio"]

_DEFAULT_SCENARIO = {
    "initialize_result": {
        "userAgent": "codex-cli fake (testing)",
        "codexHome": os.environ.get("CODEX_HOME", ""),
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
    "rate_limits_error": None,
    "initialize_delay_sec": 0.0,
    "rate_limits_delay_sec": 0.0,
    "hang_initialize": False,
    "hang_rate_limits": False,
    "exit_before_handshake": False,
    "exit_code": 1,
    "garbage_before_rate_limits": False,
    "push_notification_before_rate_limits": None,
    "server_request_before_rate_limits": None,
    "reject_before_initialize": True,
}

_METHOD_NOT_FOUND = -32601
_SERVER_NOT_INITIALIZED = -32002


def _load_scenario() -> dict:
    path = os.environ.get("FAKE_APP_SERVER_SCRIPT")
    scenario = dict(_DEFAULT_SCENARIO)
    if path:
        with open(path, encoding="utf-8") as handle:
            scenario.update(json.load(handle))
    return scenario


class _Logger:
    def __init__(self) -> None:
        self._path = os.environ.get("FAKE_APP_SERVER_LOG")

    def log(self, direction: str, message: object) -> None:
        if not self._path:
            return
        entry = {"ts": time.time(), "dir": direction, "msg": message}
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main() -> int:
    if sys.argv[1 : 1 + len(ARGV_EXPECTED)] != ARGV_EXPECTED:
        sys.stderr.write(f"fake app-server: expected argv {ARGV_EXPECTED}, got {sys.argv[1:]}\n")
        return 2
    logger = _Logger()
    logger.log("env", {"CODEX_HOME": os.environ.get("CODEX_HOME", "")})
    scenario = _load_scenario()
    if scenario["exit_before_handshake"]:
        sys.stderr.write("fake app-server: exiting before handshake (scenario)\n")
        return int(scenario["exit_code"])

    out = sys.stdout
    initialized = False

    def send(message: dict) -> None:
        logger.log("sent", message)
        out.write(json.dumps(message, ensure_ascii=False) + "\n")
        out.flush()

    def reply(request_id: object, result: object = None, error: dict | None = None) -> None:
        message = {"id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result
        send(message)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        # 每条消息重读剧本:常驻连接(Phase 4 连接池)下测试可中途改剧本
        scenario = _load_scenario()
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            logger.log("recv_bad", line)
            continue
        logger.log("recv", message)
        if not isinstance(message, dict):
            continue

        method = message.get("method")
        if method is not None and "id" in message:
            # 客户端请求
            if method == "initialize":
                if scenario["hang_initialize"]:
                    time.sleep(3600)
                    return 0
                if scenario["initialize_delay_sec"]:
                    time.sleep(float(scenario["initialize_delay_sec"]))
                reply(message["id"], scenario["initialize_result"])
            elif not initialized and scenario["reject_before_initialize"]:
                reply(
                    message["id"],
                    error={"code": _SERVER_NOT_INITIALIZED, "message": "not initialized"},
                )
            elif method == "account/rateLimits/read":
                if scenario["hang_rate_limits"]:
                    time.sleep(3600)
                    return 0
                if scenario["garbage_before_rate_limits"]:
                    out.write("<<<this is not json>>>\n")
                    out.flush()
                if scenario["push_notification_before_rate_limits"]:
                    send(scenario["push_notification_before_rate_limits"])
                if scenario["server_request_before_rate_limits"]:
                    send(scenario["server_request_before_rate_limits"])
                if scenario["rate_limits_delay_sec"]:
                    time.sleep(float(scenario["rate_limits_delay_sec"]))
                if scenario["rate_limits_error"]:
                    reply(message["id"], error=scenario["rate_limits_error"])
                else:
                    reply(message["id"], scenario["rate_limits_result"])
            else:
                reply(
                    message["id"],
                    error={"code": _METHOD_NOT_FOUND, "message": f"no such method {method}"},
                )
        elif method == "initialized":
            # 客户端通知:握手完成标志(其余通知无需处理)
            initialized = True
        # 其余 = 其他通知或客户端对我方反向请求的响应:_Logger 已记录,无需处理
    return 0


if __name__ == "__main__":
    sys.exit(main())
