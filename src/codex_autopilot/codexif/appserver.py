"""App Server 只读客户端(P3-T3;quota-monitoring §4、queue-safety §7)。

协议事实(phase0-research §2.2,源码确认 + 参考项目):
- `codex app-server --stdio` 子进程,**NDJSON**(每行一条消息,行上限 ~1MiB);
- 类 JSON-RPC 但**无 jsonrpc 字段**:请求 `{id,method,params?}`、响应
  `{id,result}` / `{id,error}`、通知 `{method,params?}`;
- 握手:`initialize`(clientInfo + capabilities)→ 结果 → `initialized` 通知;
- 响应按 id 匹配;服务端反向请求一律以 -32601 拒绝。

安全红线(FR-07-A1/A2,queue-safety §7/§8):
- 方法白名单硬编码,仅 `initialize` 与 `account/rateLimits/read`(只读);
- `_send_request` 是唯一请求出口,入口处强制白名单校验;客户端不存在
  发起任意 method 的公共 API(T-GUARD-02 源码扫描锁定);
- 本模块不写任何 Codex 状态,不读取 auth.json。

超时:每次调用独立限时(默认 25s,参考项目同款),超时后连接视为不可用。
子进程生命周期:本类为一次性连接(start→read→stop);常驻复用与空闲回收
属守护进程调度(D-QM-5,Phase 4)。
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_CALL_TIMEOUT_SEC = 25.0
MAX_LINE_BYTES = 1024 * 1024  # NDJSON 单行上限(协议实测约束)
_SHUTDOWN_GRACE_SEC = 5.0
_STDERR_TAIL_BYTES = 2048

# 只读方法白名单(硬编码;扩展必须走设计评审 + 测试更新,FR-07-A1)
READ_METHODS = frozenset({"initialize", "account/rateLimits/read"})

METHOD_INITIALIZE = "initialize"
METHOD_RATE_LIMITS = "account/rateLimits/read"

# 服务端通知 method 名(观察用,非请求;协议字符串集中本模块,T-GUARD-02)
NOTIFICATION_RATE_LIMITS_UPDATED = "account/rateLimits/updated"

_METHOD_NOT_FOUND = -32601


class AppServerError(Exception):
    """App Server 客户端故障(启动/协议/进程退出;连接此后不可用)。"""


class AppServerLaunchError(AppServerError):
    """子进程未能启动(spawn 失败/立即退出)。"""


class AppServerTimeoutError(AppServerError):
    """调用超时(服务端未在限时内响应)。"""


def _shorten(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


class AppServerClient:
    """单连接只读客户端。用例:`with AppServerClient(...) as c: data = c.read_rate_limits()`。"""

    def __init__(
        self,
        *,
        binary: str,
        codex_home: Path,
        client_version: str,
        call_timeout_sec: float = DEFAULT_CALL_TIMEOUT_SEC,
        shutdown_grace_sec: float = _SHUTDOWN_GRACE_SEC,
    ):
        if call_timeout_sec <= 0:
            raise ValueError("call_timeout_sec must be positive")
        self._binary = binary
        self._codex_home = Path(codex_home)
        self._client_version = client_version
        self._timeout = float(call_timeout_sec)
        self._grace = float(shutdown_grace_sec)
        self._proc: subprocess.Popen[bytes] | None = None
        # stderr 重定向到临时文件而非 PIPE:服务端大量写 stderr 也不会塞满管道
        # 阻塞进程;生命周期与实例绑定,stop() 统一关闭。SIM115:类管理资源。
        self._stderr_file = tempfile.TemporaryFile()  # noqa: SIM115
        self._buf = bytearray()
        self._next_id = 1
        self._started = False
        self._dead = False
        self.user_agent: str | None = None  # initialize 结果的 userAgent(诊断用)
        self.notifications: list[dict[str, Any]] = []  # 服务端通知(E4 信号,只记录)
        self.rejected_server_requests: list[str] = []  # 被拒的服务端反向请求 method 名

    # -- 生命周期 ---------------------------------------------------------

    @property
    def usable(self) -> bool:
        """已握手且未标记死亡(常驻复用判定,quota-monitoring §4/D-QM-5)。"""

        return self._started and not self._dead

    def start(self) -> None:
        """spawn 子进程并完成 initialize 握手。失败抛 AppServerLaunchError。"""

        if self._dead or self._proc is not None:
            raise AppServerError("client already started or unusable")
        argv = [self._binary, "app-server", "--stdio"]
        env = dict(os.environ)
        env["CODEX_HOME"] = str(self._codex_home)
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr_file,
                env=env,
            )
        except OSError as exc:
            self._dead = True
            raise AppServerLaunchError(f"cannot spawn app-server {self._binary!r}: {exc}") from exc
        try:
            result = self._send_request(
                METHOD_INITIALIZE,
                {
                    "clientInfo": {"name": "codex-autopilot", "version": self._client_version},
                    "capabilities": {"experimentalApi": True},
                },
            )
        except AppServerError:
            self._dead = True
            raise
        if not isinstance(result, dict):
            self._dead = True
            raise AppServerLaunchError("initialize result is not an object")
        self.user_agent = result["userAgent"] if isinstance(result.get("userAgent"), str) else None
        self._send_notification("initialized")
        self._started = True

    def stop(self) -> None:
        """退出时序:关 stdin → 宽限等待 → terminate → kill(纯进程卫生)。"""

        proc = self._proc
        self._proc = None
        self._dead = True
        if proc is None:
            self._stderr_file.close()
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            try:
                proc.wait(timeout=self._grace)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=self._grace)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=self._grace)
        finally:
            for stream in (proc.stdout,):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:  # pragma: no cover - 关闭已死流
                    pass
            self._stderr_file.close()

    def __enter__(self) -> AppServerClient:
        try:
            self.start()
        except BaseException:
            # 握手失败时 with 不会调用 __exit__,必须在此兜底回收子进程/临时文件
            self.stop()
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- 白名单只读方法(唯一公共查询面) ---------------------------------

    def read_rate_limits(self) -> dict[str, Any]:
        """`account/rateLimits/read` → 原始 result(解析在 quota/parser)。"""

        if not self._started or self._dead:
            raise AppServerError("client not started or unusable")
        try:
            result = self._send_request(METHOD_RATE_LIMITS, {})
        except AppServerError:
            self._dead = True  # 超时/协议错后连接状态不可信,弃用
            raise
        if not isinstance(result, dict):
            self._dead = True
            raise AppServerError("rateLimits result is not an object")
        return result

    # -- 请求/响应机制(内部;白名单强制) -------------------------------

    def _send_request(self, method: str, params: dict[str, Any]) -> Any:
        """发送请求并等待同 id 响应。白名单外方法直接拒绝(编程错误级)。"""

        if method not in READ_METHODS:
            raise AppServerError(f"method {method!r} is not in the read-only whitelist")
        request_id = self._next_id
        self._next_id += 1
        self._write_line({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self._timeout
        while True:
            message = self._read_message(deadline)
            if message.get("id") != request_id or "method" in message:
                self._handle_side_message(message)
                continue
            if "error" in message:
                error = message["error"]
                code = error.get("code") if isinstance(error, dict) else None
                message_text = error.get("message") if isinstance(error, dict) else "unknown error"
                raise AppServerError(
                    f"app-server error for {method}: code={code} {_shorten(str(message_text))}"
                )
            return message.get("result")

    def _send_notification(self, method: str) -> None:
        self._write_line({"method": method})

    def _handle_side_message(self, message: dict[str, Any]) -> None:
        """响应等待期间到达的服务端消息:通知记录;反向请求一律 -32601 拒绝。"""

        if "method" in message:
            method = message.get("method")
            method_name = method if isinstance(method, str) else "<invalid>"
            if "id" in message:
                self.rejected_server_requests.append(method_name)
                self._write_line(
                    {
                        "id": message.get("id"),
                        "error": {
                            "code": _METHOD_NOT_FOUND,
                            "message": (
                                "codex-autopilot is a read-only client;"
                                " server-to-client requests are not served"
                            ),
                        },
                    }
                )
            else:
                self.notifications.append(message)
                del self.notifications[:-100]  # 有界
        # 同 id 响应重复/迟到:忽略

    # -- NDJSON 传输 ------------------------------------------------------

    def _write_line(self, message: dict[str, Any]) -> None:
        proc = self._require_proc()
        assert proc.stdin is not None  # PIPE 已建
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_LINE_BYTES:  # pragma: no cover - 我方请求都很小
            raise AppServerError("outgoing message exceeds NDJSON line limit")
        try:
            proc.stdin.write(data + b"\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            exit_code = proc.poll()
            if exit_code is not None:
                raise AppServerError(
                    f"app-server exited early (exit={exit_code}, stderr: {self._stderr_tail()})"
                ) from exc
            raise AppServerError(f"app-server stdin closed: {exc}") from exc

    def _require_proc(self) -> subprocess.Popen[bytes]:
        if self._proc is None or self._dead:
            raise AppServerError("app-server process is not running")
        return self._proc

    def _read_message(self, deadline: float) -> dict[str, Any]:
        """读下一条完整 NDJSON 消息;通知/请求/响应统一返回,由调用方分派。"""

        while True:
            newline = self._buf.find(b"\n")
            if newline >= 0:
                line = bytes(self._buf[:newline])
                del self._buf[: newline + 1]
                return self._parse_line(line)
            if len(self._buf) > MAX_LINE_BYTES:
                raise AppServerError("inbound NDJSON line exceeds 1MiB limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerTimeoutError(
                    f"app-server did not answer within {self._timeout:.0f}s"
                )
            self._fill_buffer(remaining)

    def _fill_buffer(self, timeout_sec: float) -> None:
        proc = self._require_proc()
        assert proc.stdout is not None  # PIPE 已建
        fd = proc.stdout.fileno()
        ready, _, _ = select.select([fd], [], [], timeout_sec)
        if not ready:
            raise AppServerTimeoutError(f"app-server did not answer within {self._timeout:.0f}s")
        try:
            chunk = os.read(fd, 65536)
        except OSError as exc:
            raise AppServerError(f"app-server stdout read failed: {exc}") from exc
        if not chunk:
            raise AppServerError(
                f"app-server exited while waiting for response (exit={proc.poll()},"
                f" stderr: {self._stderr_tail()})"
            )
        self._buf.extend(chunk)

    def _parse_line(self, line: bytes) -> dict[str, Any]:
        if not line.strip():
            return {}  # 空行:让分派逻辑自然忽略(无 id 无 method)
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppServerError(f"app-server sent a non-JSON line: {exc}") from exc
        if not isinstance(message, dict):
            raise AppServerError("app-server sent a non-object NDJSON line")
        if "id" not in message and "method" not in message:
            raise AppServerError(
                "app-server message has neither id nor method (protocol violation)"
            )
        return message

    def _stderr_tail(self) -> str:
        try:
            self._stderr_file.flush()
            size = self._stderr_file.tell()
            self._stderr_file.seek(max(0, size - _STDERR_TAIL_BYTES))
            return _shorten(self._stderr_file.read().decode("utf-8", errors="replace"), 400)
        except OSError:  # pragma: no cover - temp file 常规可读
            return "<stderr unavailable>"
