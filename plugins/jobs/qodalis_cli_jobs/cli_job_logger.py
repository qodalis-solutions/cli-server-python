"""Job logger that captures log entries in-memory."""

from __future__ import annotations

import datetime

from qodalis_cli_server_abstractions.jobs import ICliJobLogger, JobLogEntry


class CliJobLogger(ICliJobLogger):
    """Concrete logger that captures log entries in a list."""

    def __init__(self) -> None:
        self._entries: list[JobLogEntry] = []

    @property
    def entries(self) -> list[JobLogEntry]:
        """Return the captured log entries."""
        return self._entries

    def debug(self, message: str) -> None:
        """Append a debug-level log entry."""
        self._entries.append(
            JobLogEntry(
                timestamp=datetime.datetime.now(datetime.UTC),
                level="debug",
                message=message,
            )
        )

    def info(self, message: str) -> None:
        """Append an info-level log entry."""
        self._entries.append(
            JobLogEntry(
                timestamp=datetime.datetime.now(datetime.UTC),
                level="info",
                message=message,
            )
        )

    def warning(self, message: str) -> None:
        """Append a warning-level log entry."""
        self._entries.append(
            JobLogEntry(
                timestamp=datetime.datetime.now(datetime.UTC),
                level="warning",
                message=message,
            )
        )

    def error(self, message: str) -> None:
        """Append an error-level log entry."""
        self._entries.append(
            JobLogEntry(
                timestamp=datetime.datetime.now(datetime.UTC),
                level="error",
                message=message,
            )
        )
