"""Concrete execution context for job runs."""

from __future__ import annotations

from qodalis_cli_server_abstractions.jobs import ICliJobExecutionContext, ICliJobLogger

from .cli_job_logger import CliJobLogger


class CliJobExecutionContext(ICliJobExecutionContext):
    """Concrete execution context wrapping a per-execution logger."""

    def __init__(self) -> None:
        self._logger = CliJobLogger()

    @property
    def logger(self) -> ICliJobLogger:
        """Return the logger for this execution."""
        return self._logger

    @property
    def log_entries(self) -> list:
        """Convenience accessor for the captured log entries."""
        return self._logger.entries
