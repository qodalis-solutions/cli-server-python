"""Concrete execution context for job runs."""

from __future__ import annotations

from typing import Any

from qodalis_cli_server_abstractions.jobs import ICliJobExecutionContext, ICliJobLogger

from .cli_job_logger import CliJobLogger


class CliJobExecutionContext(ICliJobExecutionContext):
    """Concrete execution context wrapping a per-execution logger."""

    def __init__(self) -> None:
        self._logger = CliJobLogger()
        self._properties: dict[str, Any] = {}
        self._discarded = False

    @property
    def logger(self) -> ICliJobLogger:
        """Return the logger for this execution."""
        return self._logger

    @property
    def properties(self) -> dict[str, Any]:
        """The run's own values, saved with the execution."""
        return self._properties

    def discard(self) -> None:
        """Leave this run out of the history when it completes."""
        self._discarded = True

    @property
    def discarded(self) -> bool:
        """Whether the job asked to leave this run out of the history."""
        return self._discarded

    @property
    def log_entries(self) -> list:
        """Convenience accessor for the captured log entries."""
        return self._logger.entries
