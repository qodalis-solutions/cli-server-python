from __future__ import annotations

import abc
import datetime

from .job_execution import JobExecution
from .job_execution_query import JobExecutionQuery
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

    async def query_executions(
        self, query: JobExecutionQuery
    ) -> tuple[list[JobExecution], int]:
        """Executions of several jobs (or of every job), newest first, as
        ``(items, total_count)``.  The default merges :meth:`get_executions`
        per job and needs ``query.job_ids`` (it raises
        :class:`NotImplementedError` without them); override it to query the
        store once."""
        if query.job_ids is None:
            raise NotImplementedError(
                "Listing the executions of every job is not supported by this store"
            )

        async def list_job(job_id: str, limit: int, status: str | None):
            return await self.get_executions(job_id, limit=limit, offset=0, status=status)

        return await query.merge(query.job_ids, list_job)

    async def prune(self, before: datetime.datetime) -> int:
        """Deletes the executions (and their logs) that started before
        ``before``; returns how many.  Run it on a schedule to keep the history
        bounded.  The default keeps everything and returns 0."""
        return 0

    async def delete_execution(self, execution_id: str) -> bool:
        """Deletes an execution (a run the job discarded, see
        ``ICliJobExecutionContext.discard``); returns whether it was deleted.
        The default keeps every execution and returns ``False``."""
        return False

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
