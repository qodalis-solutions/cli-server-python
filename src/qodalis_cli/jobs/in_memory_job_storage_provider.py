from __future__ import annotations

from qodalis_cli_server_abstractions.jobs import (
    ICliJobStorageProvider,
    JobExecution,
    JobState,
)


class InMemoryJobStorageProvider(ICliJobStorageProvider):
    """In-memory implementation — data is lost on server restart."""

    def __init__(self) -> None:
        # job_id -> list of executions (newest first)
        self._executions: dict[str, list[JobExecution]] = {}
        # execution_id -> execution
        self._executions_by_id: dict[str, JobExecution] = {}
        # job_id -> JobState
        self._states: dict[str, JobState] = {}

    async def save_execution(self, execution: JobExecution) -> None:
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
        all_items = self._executions.get(job_id, [])
        if status:
            all_items = [e for e in all_items if e.status == status]
        total = len(all_items)
        items = all_items[offset : offset + limit]
        return items, total

    async def get_execution(self, execution_id: str) -> JobExecution | None:
        return self._executions_by_id.get(execution_id)

    async def save_job_state(self, job_id: str, state: JobState) -> None:
        self._states[job_id] = state

    async def get_job_state(self, job_id: str) -> JobState | None:
        return self._states.get(job_id)

    async def get_all_job_states(self) -> dict[str, JobState]:
        return dict(self._states)
