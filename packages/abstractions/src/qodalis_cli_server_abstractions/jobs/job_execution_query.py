from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

from .job_execution import JobExecution

#: Lists one job's executions: ``(job_id, limit, status) -> (items, total)``.
ListJobExecutions = Callable[
    [str, int, "str | None"], Awaitable["tuple[list[JobExecution], int]"]
]


def as_utc(value: datetime.datetime) -> datetime.datetime:
    """``value`` as an aware UTC datetime (naive values are taken as UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


@dataclass
class JobExecutionQuery:
    """Which executions to list across jobs
    (``GET /api/v1/qcli/jobs/executions``), newest first."""

    #: Only the executions of these jobs; ``None`` for every job.
    job_ids: list[str] | None = None
    #: Only executions with this status (e.g. ``failed``), case-insensitive.
    status: str | None = None
    #: Only executions started at or after this time.
    since: datetime.datetime | None = None
    #: Maximum number of executions to return.
    limit: int = 20
    #: Number of executions to skip.
    offset: int = 0

    def matches(self, execution: JobExecution) -> bool:
        """Whether ``execution`` passes the job, status and since filters."""
        if self.job_ids is not None and execution.job_id not in self.job_ids:
            return False
        if self.status and execution.status.lower() != self.status.lower():
            return False
        if self.since is not None and as_utc(execution.started_at) < as_utc(self.since):
            return False
        return True

    async def merge(
        self, job_ids: Iterable[str], list_job: ListJobExecutions
    ) -> tuple[list[JobExecution], int]:
        """Lists the executions of several jobs from a per-job listing: reads
        the first ``offset + limit`` of each job (all of them with ``since``)
        and merges them newest first.  The default for stores and managers
        without a query of their own."""
        merged: list[JobExecution] = []
        total = 0
        size = self.offset + self.limit if self.since is None else 2**31 - 1
        status = self.status.lower() if self.status else None
        for job_id in dict.fromkeys(job_ids):
            items, count = await list_job(job_id, size, status)
            if self.since is None:
                merged.extend(items)
                total += count
            else:
                since = as_utc(self.since)
                matching = [e for e in items if as_utc(e.started_at) >= since]
                merged.extend(matching)
                total += len(matching)
        merged.sort(key=lambda e: as_utc(e.started_at), reverse=True)
        return merged[self.offset: self.offset + self.limit], total
