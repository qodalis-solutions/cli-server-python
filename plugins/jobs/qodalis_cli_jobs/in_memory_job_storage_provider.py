"""In-memory job storage provider."""

from __future__ import annotations

import datetime

from qodalis_cli_server_abstractions.jobs import (
    ICliJobStorageProvider,
    JobExecution,
    JobExecutionQuery,
    JobState,
)
from qodalis_cli_server_abstractions.jobs.job_execution_query import as_utc


class InMemoryJobStorageProvider(ICliJobStorageProvider):
    """In-memory implementation — data is lost on server restart."""

    def __init__(self) -> None:
        self._executions: dict[str, list[JobExecution]] = {}
        self._executions_by_id: dict[str, JobExecution] = {}
        self._states: dict[str, JobState] = {}

    async def save_execution(self, execution: JobExecution) -> None:
        """Persist or update a job execution record."""
        self._executions_by_id[execution.id] = execution
        job_list = self._executions.setdefault(execution.job_id, [])
        # Update if existing, else prepend
        for i, existing in enumerate(job_list):
            if existing.id == execution.id:
                job_list[i] = execution
                return
        job_list.insert(0, execution)

    async def get_executions(
        self,
        job_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
    ) -> tuple[list[JobExecution], int]:
        """Return a paginated list of executions for a job and the total count."""
        all_items = self._executions.get(job_id, [])
        if status:
            all_items = [e for e in all_items if e.status == status]
        total = len(all_items)
        items = all_items[offset : offset + limit]
        return items, total

    async def query_executions(
        self, query: JobExecutionQuery
    ) -> tuple[list[JobExecution], int]:
        """Executions across jobs (every job when ``query.job_ids`` is ``None``),
        newest first."""
        matches = [e for e in self._executions_by_id.values() if query.matches(e)]
        matches.sort(key=lambda e: as_utc(e.started_at), reverse=True)
        return matches[query.offset: query.offset + query.limit], len(matches)

    async def prune(self, before: datetime.datetime) -> int:
        """Delete the executions that started before ``before``."""
        cutoff = as_utc(before)
        old = [e.id for e in self._executions_by_id.values() if as_utc(e.started_at) < cutoff]
        for execution_id in old:
            await self.delete_execution(execution_id)
        return len(old)

    async def delete_execution(self, execution_id: str) -> bool:
        """Delete one execution; ``False`` when there is none."""
        execution = self._executions_by_id.pop(execution_id, None)
        if execution is None:
            return False
        job_list = self._executions.get(execution.job_id, [])
        job_list[:] = [e for e in job_list if e.id != execution_id]
        return True

    async def get_execution(self, execution_id: str) -> JobExecution | None:
        """Return a single execution by ID, or ``None``."""
        return self._executions_by_id.get(execution_id)

    async def save_job_state(self, job_id: str, state: JobState) -> None:
        """Persist the current state of a job."""
        self._states[job_id] = state

    async def get_job_state(self, job_id: str) -> JobState | None:
        """Return the persisted state for a job, or ``None``."""
        return self._states.get(job_id)

    async def get_all_job_states(self) -> dict[str, JobState]:
        """Return all persisted job states."""
        return dict(self._states)
