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
