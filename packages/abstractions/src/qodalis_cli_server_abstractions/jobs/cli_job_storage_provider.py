from __future__ import annotations

import abc
import datetime
from collections.abc import Mapping

from .job_execution import JobExecution
from .job_execution_query import JobExecutionQuery, as_utc
from .job_execution_summary import JobExecutionSummary, summarize_job_runs
from .job_history_retention import JobHistoryLimits
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
        """Return executions for a job as ``(items, total_count)``.  A store
        may leave the logs out (setting ``log_count``); :meth:`get_execution`
        returns a run with its logs.

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

    async def summarize_executions(
        self, since: datetime.datetime, job_ids: list[str] | None = None
    ) -> list[JobExecutionSummary]:
        """Per job and status, how many runs started at or after ``since`` and
        when the newest one started ("last success per job", "runs in the last
        24 hours") without loading the runs.  Only the jobs of ``job_ids``, or
        every job.  The default pages through :meth:`query_executions`
        (without logs; it needs ``job_ids`` when that is the default too) and
        groups in memory; database stores override it with a ``GROUP BY``."""
        page_size = 500
        runs: list[JobExecution] = []
        offset = 0
        while True:
            items, total = await self.query_executions(JobExecutionQuery(
                job_ids=job_ids, since=as_utc(since), limit=page_size, offset=offset,
            ))
            runs.extend(items)
            offset += len(items)
            if len(items) < page_size or offset >= total:
                break
        return summarize_job_runs(runs)

    async def prune(self, before: datetime.datetime) -> int:
        """Deletes the executions (and their logs) that started before
        ``before``; returns how many.  Run it on a schedule to keep the history
        bounded.  The default keeps everything and returns 0."""
        return 0

    async def prune_history(
        self,
        limits: Mapping[str, JobHistoryLimits | None],
        default: JobHistoryLimits | None,
        now: datetime.datetime,
    ) -> dict[str, int]:
        """Deletes the runs (and their logs) that break their job's history
        retention at ``now``; returns how many per job id (jobs with none
        deleted may be left out).

        ``limits`` holds the limits of each registered job (``None``: keep
        every run of that job); runs of any other job id (jobs no longer
        registered) follow ``default`` (``None``: keep them).  A run breaks the
        limits when older than the max age or not among the newest max runs of
        its job; failed runs (status ``failed``) use the ``failed_*`` limits
        when set.  A running execution or a job's latest run is never deleted.
        ``JobHistoryLimits.expired_ids`` selects them in memory; a database
        store should delete set-based instead.

        Optional: the jobs plugin's history retention calls it, and falls back
        to :meth:`prune` with the global max age (no count limits, per-job
        overrides or kept latest run) for a store that keeps this default,
        which raises :class:`NotImplementedError`."""
        raise NotImplementedError("This store does not prune by retention limits")

    async def delete_execution(self, execution_id: str) -> bool:
        """Deletes an execution and its logs (a run the job discarded, see
        ``ICliJobExecutionContext.discard``, or a run deleted through
        ``DELETE /api/v1/qcli/jobs/{id}/history``); returns whether it was
        deleted.  The default keeps every execution and returns ``False``; the
        scheduler answers "not supported" for deletions while a store keeps it."""
        return False

    @abc.abstractmethod
    async def get_execution(self, execution_id: str) -> JobExecution | None:
        """Retrieve a single execution by its identifier, with its logs."""
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
