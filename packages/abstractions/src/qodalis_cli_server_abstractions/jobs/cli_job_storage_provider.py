from __future__ import annotations

import abc

from .job_execution import JobExecution
from .job_state import JobState


class ICliJobStorageProvider(abc.ABC):
    """Pluggable storage for job execution history and persisted state."""

    @abc.abstractmethod
    async def save_execution(self, execution: JobExecution) -> None: ...

    @abc.abstractmethod
    async def get_executions(
        self,
        job_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
    ) -> tuple[list[JobExecution], int]:
        """Return (items, total) for the given job, with optional status filter."""
        ...

    @abc.abstractmethod
    async def get_execution(self, execution_id: str) -> JobExecution | None: ...

    @abc.abstractmethod
    async def save_job_state(self, job_id: str, state: JobState) -> None: ...

    @abc.abstractmethod
    async def get_job_state(self, job_id: str) -> JobState | None: ...

    @abc.abstractmethod
    async def get_all_job_states(self) -> dict[str, JobState]: ...
