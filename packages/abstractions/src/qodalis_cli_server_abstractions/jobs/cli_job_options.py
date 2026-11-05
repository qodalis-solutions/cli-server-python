from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CliJobOptions:
    """Options controlling how a job is scheduled and executed."""

    name: str | None = None
    description: str | None = None
    group: str | None = None
    schedule: str | None = None  # cron expression
    interval: str | None = None  # e.g. "30s", "5m", "1h", "1d"
    enabled: bool = True
    max_retries: int = 1
    retry_delay: str = "5s"  # delay before first retry
    retry_strategy: str = "exponential"  # 'fixed' | 'linear' | 'exponential'
    timeout: str | None = None  # e.g. "5m"
    overlap_policy: str = "skip"  # 'skip' | 'queue' | 'cancel'
    #: The job's id in the jobs API, its history and its saved state (pause,
    #: last run), e.g. ``fetch-rates:bnr``.  Keep it stable so a restart finds
    #: the job's history and state again.  Default: the name, with ``-2``,
    #: ``-3``, ... for further jobs of the same name, in registration order.
    id: str | None = None
    #: Runs the job once right after the scheduler starts, whatever its
    #: schedule (also for jobs that only run on demand).  Not for paused or
    #: stopped jobs.  An interval job keeps its rhythm across restarts: the
    #: next run is the last run plus the interval (right away when that has
    #: passed).
    run_on_start: bool = False
