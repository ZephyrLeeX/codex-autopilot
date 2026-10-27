"""rollout JSONL 增量解析器(P3-T1;FR-08-A8、S-23、R-06)。

协议事实(phase0-research §2.3 实证):
- 每行一个 JSON 对象,顶层 `{"timestamp": ISO-8601, "type": …, "payload": …}`;
- 顶层 type:session_meta / turn_context / response_item / event_msg /
  token_usage_record / world_state(未知 type 容忍跳过);
- `event_msg.payload.type` ∈ task_started / item_completed / token_count /
  task_complete / thread_settings_applied…;额度快照在 token_count 的
  `rate_limits` 字段(解析在 quota/parser.py,本模块只做事件模型)。

红线:
- **只读**,绝不写入/截断/重命名 Codex 原始文件;
- 撕裂尾行(写入方尚未写完,无换行符)不消费、不解析——游标停在行首,
  下次补读(FR-08-A8:不基于不完整文件做任何判定);
- 中途坏行(有换行但非 JSON)跳过并计数,不中断后续行;
- 本模块不落日志 payload 内容(NFR-09:会话正文不出现在日志)。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 单行字节上限:内容超限视为损坏行跳过(防无换行巨块占内存;正常
# response_item 行远小于此)。
MAX_LINE_BYTES = 16 * 1024 * 1024
_READ_CHUNK = 256 * 1024

KNOWN_TOP_LEVEL_TYPES = frozenset(
    {
        "session_meta",
        "turn_context",
        "response_item",
        "event_msg",
        "token_usage_record",
        "world_state",
    }
)


class RolloutError(Exception):
    """rollout 读取失败(文件缺失/不可读/IO 错误)。"""


@dataclass(frozen=True)
class RolloutEvent:
    """一行 rollout 事件的结构化视图(payload 为原始映射,仅内存传递)。"""

    type: str  # 顶层 type(未知类型原样保留)
    payload_type: str | None  # payload.type(无嵌套 type 时 None)
    timestamp: int | None  # Unix 秒(UTC);缺失/不可解析为 None
    payload: dict[str, Any]
    offset: int  # 行首字节偏移(调试/游标对账)
    line_no: int

    @property
    def is_token_count(self) -> bool:
        return self.type == "event_msg" and self.payload_type == "token_count"

    def rate_limits_payload(self) -> dict[str, Any] | None:
        """token_count 事件的 rate_limits 载荷;非 token_count 或缺失返回 None。"""

        if not self.is_token_count:
            return None
        data = self.payload.get("rate_limits")
        return data if isinstance(data, dict) else None

    @property
    def is_user_message(self) -> bool:
        """用户消息(response_item.message 且 role=user;G3 人工介入检测用,Phase 5)。"""

        if self.type != "response_item":
            return False
        payload = self.payload
        return payload.get("type") == "message" and payload.get("role") == "user"


def parse_event_timestamp(raw: Any) -> int | None:
    """顶层 timestamp(ISO-8601)→ Unix 秒。naive 视为 UTC(Codex 写 Z 后缀)。"""

    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def parse_line(line: str, *, offset: int = 0, line_no: int = 0) -> RolloutEvent | None:
    """单行 JSON → 事件;空行/非对象/坏 JSON 返回 None(调用方计警告)。"""

    text = line.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    etype = obj.get("type")
    payload = obj.get("payload")
    if not isinstance(etype, str) or not isinstance(payload, dict):
        # rollout 行必有 type/payload;形状不符按坏行处理(跳过,不猜)
        return None
    payload_type = payload.get("type") if isinstance(payload.get("type"), str) else None
    return RolloutEvent(
        type=etype,
        payload_type=payload_type,
        timestamp=parse_event_timestamp(obj.get("timestamp")),
        payload=payload,
        offset=offset,
        line_no=line_no,
    )


@dataclass
class _ReadStats:
    torn_tail_lines: int = 0  # 撕裂尾行(无换行,未消费)
    bad_lines: int = 0  # 坏行(有换行,已跳过)
    truncated_lines: int = 0  # 超长行(已跳过)


@dataclass
class RolloutReader:
    """单文件增量读取器:字节游标在内存,新进程从 0 重建(quota-monitoring §6)。

    用法:`events = reader.read_new_events()`;重复调用只返回新增行。
    每实例绑定一个文件;文件被截断/替换(size < 游标)时重置游标重读并告警。
    """

    path: Path
    _offset: int = field(default=0, repr=False)
    stats: _ReadStats = field(default_factory=_ReadStats, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    @property
    def offset(self) -> int:
        return self._offset

    def read_new_events(self) -> list[RolloutEvent]:
        """读取游标之后的新事件(只到最近一个完整行为止)。"""

        try:
            size = self.path.stat().st_size
        except OSError as exc:
            raise RolloutError(f"cannot stat rollout file {self.path}: {exc}") from exc
        if size < self._offset:
            logger.warning("rollout file shrank (rotated?): %s; re-reading from start", self.path)
            self._offset = 0
        if size == self._offset:
            return []
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                events, consumed = self._drain(handle, line_no_start=0)
        except OSError as exc:
            raise RolloutError(f"cannot read rollout file {self.path}: {exc}") from exc
        self._offset += consumed
        return events

    # -- 内部 -------------------------------------------------------------

    def _drain(self, handle: Any, *, line_no_start: int) -> tuple[list[RolloutEvent], int]:
        """从当前位置读到 EOF,返回(事件, 可消费字节数)。

        可消费字节数停在最后一个**完整行**(以 \\n 结尾)之后;无换行的
        尾部字节属于撕裂行,不计入(下次重读,FR-08-A8)。
        """

        events: list[RolloutEvent] = []
        consumed = 0
        line_start = 0  # 相对本次读取起点的行首偏移
        buf = bytearray()
        line_no = line_no_start
        while True:
            chunk = handle.read(_READ_CHUNK)
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line_bytes = bytes(buf[: nl + 1])
                del buf[: nl + 1]
                consumed += len(line_bytes)
                event = self._consume_line(line_bytes, offset=line_start, line_no=line_no)
                if event is not None:
                    events.append(event)
                line_start += len(line_bytes)
                line_no += 1
        if buf:
            self.stats.torn_tail_lines += 1
            logger.warning(
                "rollout tail line incomplete (no newline), will re-read later: %s", self.path
            )
        return events, consumed

    def _consume_line(self, line_bytes: bytes, *, offset: int, line_no: int) -> RolloutEvent | None:
        raw = line_bytes.decode("utf-8", errors="replace")
        if len(line_bytes) > MAX_LINE_BYTES:
            self.stats.truncated_lines += 1
            logger.warning("rollout line exceeds %d bytes, skipped: %s", MAX_LINE_BYTES, self.path)
            return None
        event = parse_line(raw, offset=offset, line_no=line_no)
        if event is None:
            self.stats.bad_lines += 1
            logger.warning(
                "rollout line %d is not a valid event object, skipped: %s", line_no, self.path
            )
        return event


def read_file_events(path: Path) -> list[RolloutEvent]:
    """一次性读取整个文件的全部完整行事件(无游标;CLI 单次展示用)。"""

    return RolloutReader(path).read_new_events()


def latest_rate_limits_sample(
    events: list[RolloutEvent],
) -> tuple[dict[str, Any], int | None, int] | None:
    """事件流中最后一个 token_count 样本 → (rate_limits 载荷, 事件时间, 行号)。

    无 token_count 事件返回 None。时间缺失为 None(调用方决定回退,如文件 mtime)。
    """

    sample = None
    for event in events:
        payload = event.rate_limits_payload()
        if payload is not None:
            sample = (payload, event.timestamp, event.line_no)
    return sample
