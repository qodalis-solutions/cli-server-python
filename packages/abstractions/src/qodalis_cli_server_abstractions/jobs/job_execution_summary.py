from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Iterable

from .job_execution import JobExecution
from .job_execution_query import as_utc


@dataclass(frozen=True)
class JobExecutionSummary:
    """The runs of one job with one status started in a range
    (``summarize_executions``): how many there are and when the newest one
    started, without loading the runs."""

    #: The job identifier.
    job_id: str
    #: The status of the runs counted.
    status: str
    #: The number of runs.
    count: int
    #: When the newest of these runs started (UTC).
    last_started_at: datetime.datetime


def summarize_job_runs(runs: Iterable[JobExecution]) -> list[JobExecutionSummary]:
    """Groups ``runs`` by job and status, ordered by job id and status: the
    summary of stores that hold their runs in memory, and the default of
    ``ICliJobStorageProvider.summarize_executions``."""
    groups: dict[tuple[str, str], tuple[int, datetime.datetime]] = {}
    for run in runs:
        key = (run.job_id, run.status)
        started = as_utc(run.started_at)
        count, last = groups.get(key, (0, started))
        groups[key] = (count + 1, max(last, started))
    return [
        JobExecutionSummary(job_id=job_id, status=status, count=count, last_started_at=last)
        for (job_id, status), (count, last) in sorted(groups.items())
    ]
