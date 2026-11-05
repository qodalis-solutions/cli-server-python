from __future__ import annotations

import abc
from typing import Any

from .cli_job_logger import ICliJobLogger


class ICliJobExecutionContext(abc.ABC):
    """Context provided to a job during execution."""

    @property
    @abc.abstractmethod
    def logger(self) -> ICliJobLogger:
        """Logger available to the job during execution."""
        ...

    @property
    @abc.abstractmethod
    def properties(self) -> dict[str, Any]:
        """The execution's own values (``JobExecution.properties``), e.g.
        ``context.properties["items"] = 32``; saved when the run ends,
        whatever its outcome."""
        ...

    @abc.abstractmethod
    def discard(self) -> None:
        """Leaves this run out of the history when it completes: for a
        frequent job whose run found nothing to do.  The job's last run is
        still updated.  Failed runs are always kept."""
        ...
