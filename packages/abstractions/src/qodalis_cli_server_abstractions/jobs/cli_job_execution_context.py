from __future__ import annotations

import abc

from .cli_job_logger import ICliJobLogger


class ICliJobExecutionContext(abc.ABC):
    """Context provided to a job during execution."""

    @property
    @abc.abstractmethod
    def logger(self) -> ICliJobLogger:
        """Logger available to the job during execution."""
        ...
