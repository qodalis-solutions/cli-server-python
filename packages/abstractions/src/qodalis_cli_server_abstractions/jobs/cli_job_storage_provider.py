from __future__ import annotations

import abc

from .job_execution import JobExecution
from .job_state import JobState


class ICliJobStorageProvider(abc.ABC):
    """Pluggable storage for job execution history and persisted state."""

    @abc.abstractmethod
    async def save_execution(self, execution: JobExecution) -> None:
        """Persist a job execution record."""
        ...

    @abc.abstractmethod
    async def get_executions(
        self,
        job_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
    ) -> tuple[list[JobExecution], int]:
        """Return executions for a job as ``(items, total_count)``.

        Args:
            job_id: The job identifier.
            limit: Maximum number of records to return.
            offset: Number of records to skip.
            status: Optional status filter (e.g. ``"completed"``).
        """
        ...

    @abc.abstractmethod
    async def get_execution(self, execution_id: str) -> JobExecution | None:
        """Retrieve a single execution by its identifier."""
        ...

    @abc.abstractmethod
    async def save_job_state(self, job_id: str, state: JobState) -> None:
        """Persist the current state of a job."""
        ...

    @abc.abstractmethod
    async def get_job_state(self, job_id: str) -> JobState | None:
        """Retrieve the persisted state for a job, if any."""
        ...

    @abc.abstractmethod
    async def get_all_job_states(self) -> dict[str, JobState]:
        """Return all persisted job states keyed by job identifier."""
        ...
