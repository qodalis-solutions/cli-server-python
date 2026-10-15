"""Tests for CliLogSocketManager and WebSocketLogHandler."""

from __future__ import annotations

import json
import logging

import pytest

from qodalis_cli.services.cli_log_socket_manager import (
    CliLogSocketManager,
    WebSocketLogHandler,
)


# ---------------------------------------------------------------------------
# should_send_log
# ---------------------------------------------------------------------------


class TestShouldSendLog:
    def test_none_filter_allows_everything(self) -> None:
        assert CliLogSocketManager.should_send_log(None, "debug") is True
        assert CliLogSocketManager.should_send_log(None, "fatal") is True

    def test_empty_filter_allows_everything(self) -> None:
        assert CliLogSocketManager.should_send_log("", "debug") is True

    def test_same_level_passes(self) -> None:
        assert CliLogSocketManager.should_send_log("warning", "warning") is True

    def test_higher_level_passes(self) -> None:
        assert CliLogSocketManager.should_send_log("warning", "error") is True
        assert CliLogSocketManager.should_send_log("debug", "fatal") is True

    def test_lower_level_blocked(self) -> None:
        assert CliLogSocketManager.should_send_log("error", "debug") is False
        assert CliLogSocketManager.should_send_log("warning", "information") is False

    def test_case_insensitive(self) -> None:
        assert CliLogSocketManager.should_send_log("Warning", "ERROR") is True
        assert CliLogSocketManager.should_send_log("ERROR", "Debug") is False

    def test_unknown_filter_level_passes(self) -> None:
        assert CliLogSocketManager.should_send_log("custom", "debug") is True

    def test_unknown_log_level_passes(self) -> None:
        assert CliLogSocketManager.should_send_log("error", "custom") is True

    def test_verbose_is_lowest(self) -> None:
        assert CliLogSocketManager.should_send_log("verbose", "verbose") is True
        assert CliLogSocketManager.should_send_log("verbose", "debug") is True
        assert CliLogSocketManager.should_send_log("debug", "verbose") is False


# ---------------------------------------------------------------------------
# format_log_message
# ---------------------------------------------------------------------------


class TestFormatLogMessage:
    def test_returns_valid_json(self) -> None:
        result = CliLogSocketManager.format_log_message("Information", "hello", "test.cat")
        payload = json.loads(result)
        assert payload["type"] == "log"
        assert payload["level"] == "information"
        assert payload["message"] == "hello"
        assert payload["category"] == "test.cat"
        assert "timestamp" in payload

    def test_level_is_lowercased(self) -> None:
        result = CliLogSocketManager.format_log_message("WARNING", "msg")
        payload = json.loads(result)
        assert payload["level"] == "warning"

    def test_category_defaults_to_none(self) -> None:
        result = CliLogSocketManager.format_log_message("debug", "msg")
        payload = json.loads(result)
        assert payload["category"] is None

    def test_timestamp_is_iso_format(self) -> None:
        result = CliLogSocketManager.format_log_message("error", "msg")
        payload = json.loads(result)
        # Should contain 'T' separator typical of ISO 8601
        assert "T" in payload["timestamp"]


# ---------------------------------------------------------------------------
# WebSocketLogHandler.map_log_level
# ---------------------------------------------------------------------------


class TestMapLogLevel:
    def test_debug(self) -> None:
        assert WebSocketLogHandler.map_log_level(logging.DEBUG) == "debug"

    def test_info(self) -> None:
        assert WebSocketLogHandler.map_log_level(logging.INFO) == "information"

    def test_warning(self) -> None:
        assert WebSocketLogHandler.map_log_level(logging.WARNING) == "warning"

    def test_error(self) -> None:
        assert WebSocketLogHandler.map_log_level(logging.ERROR) == "error"

    def test_critical(self) -> None:
        assert WebSocketLogHandler.map_log_level(logging.CRITICAL) == "fatal"

    def test_below_debug(self) -> None:
        assert WebSocketLogHandler.map_log_level(5) == "debug"


# ---------------------------------------------------------------------------
# WebSocketLogHandler.emit
# ---------------------------------------------------------------------------


class TestWebSocketLogHandlerEmit:
    def test_emit_calls_broadcast(self) -> None:
        manager = CliLogSocketManager()
        handler = WebSocketLogHandler(manager)

        captured: list[tuple[str, str, str | None]] = []
        original = manager.broadcast_log

        def spy(level: str, message: str, category: str | None = None) -> None:
            captured.append((level, message, category))

        manager.broadcast_log = spy  # type: ignore[assignment]

        record = logging.LogRecord(
            name="my.logger",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="something happened",
            args=None,
            exc_info=None,
        )
        handler.emit(record)

        assert len(captured) == 1
        level, message, category = captured[0]
        assert level == "warning"
        assert "something happened" in message
        assert category == "my.logger"

    def test_emit_uses_info_level_mapping(self) -> None:
        manager = CliLogSocketManager()
        handler = WebSocketLogHandler(manager)

        captured: list[tuple[str, str, str | None]] = []

        def spy(level: str, message: str, category: str | None = None) -> None:
            captured.append((level, message, category))

        manager.broadcast_log = spy  # type: ignore[assignment]

        record = logging.LogRecord(
            name="app",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="info msg",
            args=None,
            exc_info=None,
        )
        handler.emit(record)

        assert captured[0][0] == "information"


# ---------------------------------------------------------------------------
# redact
# ---------------------------------------------------------------------------


class TestRedact:
    def test_redacts_bearer_token(self) -> None:
        from qodalis_cli.security import redact

        assert redact("Authorization: Bearer abc.def") == "Authorization: Bearer [redacted]"

    def test_redacts_url_encoded_bearer_token(self) -> None:
        from qodalis_cli.security import redact

        out = redact("GET /ws/qcli/shell?_header_Authorization=Bearer%20abc.def%3D&x=1")
        assert "abc" not in out and "def" not in out
        assert "Bearer%20[redacted]&x=1" in out


# ---------------------------------------------------------------------------
# uvicorn access/error log redaction
# ---------------------------------------------------------------------------


class TestUvicornLogRedaction:
    SECRET_PATH = (
        "/ws/qcli/shell?ticket=tk-SECRET1&cmd=cat%20SECRET2&token=SECRET3"
        "&_header_Authorization=Bearer%20SECRET4&access_token=SECRET5&cols=80"
    )

    def test_redact_covers_query_secrets(self) -> None:
        from qodalis_cli.security import redact

        out = redact(self.SECRET_PATH)
        assert "SECRET" not in out, out
        assert out.endswith("&cols=80")
        assert "ticket=[redacted]" in out and "cmd=[redacted]" in out

    def test_filter_redacts_access_log_args_and_keeps_formatter_working(self) -> None:
        from uvicorn.logging import AccessFormatter

        from qodalis_cli import RedactingLogFilter

        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:5000", "GET", self.SECRET_PATH, "1.1", 200), None,
        )
        assert RedactingLogFilter().filter(record) is True
        line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(record)
        assert "SECRET" not in line, line
        assert "/ws/qcli/shell?ticket=[redacted]" in line and "200" in line

    def test_filter_redacts_websocket_accept_line(self) -> None:
        from qodalis_cli import RedactingLogFilter

        record = logging.LogRecord(
            "uvicorn.error", logging.INFO, __file__, 1,
            '%s - "WebSocket %s" [accepted]', ("127.0.0.1:5000", self.SECRET_PATH), None,
        )
        RedactingLogFilter().filter(record)
        assert "SECRET" not in record.getMessage()

    def test_install_is_idempotent_and_covers_uvicorn_loggers(self) -> None:
        from qodalis_cli import RedactingLogFilter, install_log_redaction

        install_log_redaction()
        install_log_redaction()
        try:
            for name in ("uvicorn.access", "uvicorn.error"):
                found = [f for f in logging.getLogger(name).filters if isinstance(f, RedactingLogFilter)]
                assert len(found) == 1, name
        finally:
            for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
                lg = logging.getLogger(name)
                for f in [f for f in lg.filters if isinstance(f, RedactingLogFilter)]:
                    lg.removeFilter(f)

    def test_standalone_server_installs_redaction(self, monkeypatch) -> None:
        import uvicorn

        from qodalis_cli import RedactingLogFilter
        from qodalis_cli import server

        calls = []
        monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append(kw))
        try:
            server.main()
            assert calls
            assert any(isinstance(f, RedactingLogFilter) for f in logging.getLogger("uvicorn.access").filters)
        finally:
            for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
                lg = logging.getLogger(name)
                for f in [f for f in lg.filters if isinstance(f, RedactingLogFilter)]:
                    lg.removeFilter(f)
