"""P2-T3 结构化日志测试:JSON 行格式、extras、异常。"""

from __future__ import annotations

import io
import json
import logging

from codex_autopilot.util.jsonlog import JsonFormatter, setup_logging


def _capture_records(records: list[logging.LogRecord], level=logging.INFO):
    logger = logging.getLogger("codex_autopilot.test")
    logger.setLevel(level)
    logger.addHandler(_Collector(records))
    return logger


class _Collector(logging.Handler):
    def __init__(self, records):
        super().__init__()
        self.records = records

    def emit(self, record):
        self.records.append(record)


class TestJsonFormatter:
    def test_required_fields(self):
        records: list[logging.LogRecord] = []
        logger = _capture_records(records)
        logger.info("hello %s", "world")
        line = JsonFormatter().format(records[0])
        payload = json.loads(line)
        assert payload["message"] == "hello world"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "codex_autopilot.test"
        assert payload["ts"].endswith("Z") or "+" in payload["ts"]

    def test_extras_included(self):
        records: list[logging.LogRecord] = []
        logger = _capture_records(records)
        logger.info("tick done", extra={"session_uuid": "abc", "tick_no": 7})
        payload = json.loads(JsonFormatter().format(records[0]))
        assert payload["session_uuid"] == "abc"
        assert payload["tick_no"] == 7

    def test_exception_attached(self):
        records: list[logging.LogRecord] = []
        logger = _capture_records(records)
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logger.exception("failed")
        payload = json.loads(JsonFormatter().format(records[0]))
        assert "RuntimeError: boom" in payload["exception"]


class TestSetupLogging:
    def test_single_line_json_output(self):
        stream = io.StringIO()
        setup_logging(logging.DEBUG, stream=stream, root_logger_name="codex_autopilot.setup_test")
        logger = logging.getLogger("codex_autopilot.setup_test")
        logger.warning("careful", extra={"account": "x"})
        logger.info("fine")
        lines = stream.getvalue().strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            json.loads(line)  # 每行都是合法 JSON

    def test_reconfiguration_idempotent(self):
        stream1, stream2 = io.StringIO(), io.StringIO()
        setup_logging(logging.DEBUG, stream=stream1, root_logger_name="codex_autopilot.reconf")
        setup_logging(logging.DEBUG, stream=stream2, root_logger_name="codex_autopilot.reconf")
        logging.getLogger("codex_autopilot.reconf").info("once")
        assert stream1.getvalue() == ""  # 旧 handler 已移除
        assert "once" in stream2.getvalue()
