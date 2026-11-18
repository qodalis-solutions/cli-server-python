from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Union

from .job_execution import JobExecution
from .job_execution_query import as_utc

#: A duration: a string like ``"30s"``, ``"5m"``, ``"12h"``, ``"30d"`` (the
#: format of a job's ``interval``) or a :class:`datetime.timedelta`.
Duration = Union[str, datetime.timedelta]

_DURATION = re.compile(r"^(\d+)(s|m|h|d)$")
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}

#: Statuses of the runs the ``failed_*`` limits apply to.
FAILED_STATUSES = frozenset({"failed", "timed_out"})


def parse_duration(value: Duration | None) -> datetime.timedelta | None:
    """``value`` as a :class:`datetime.timedelta` (``None`` stays ``None``).

    Raises ``ValueError`` for a string that is not a number followed by
    ``s``, ``m``, ``h`` or ``d``, or a negative duration."""
    if value is None:
        return None
    if isinstance(value, datetime.timedelta):
        if value < datetime.timedelta(0):
            raise ValueError(f"Invalid duration: {value!r} is negative")
        return value
    match = _DURATION.match(str(value).strip())
    if not match:
        raise ValueError(
            f"Invalid duration: {value!r}. "
            "Expected a number followed by s, m, h, or d (e.g. '30s', '30d')."
        )
    return datetime.timedelta(seconds=int(match.group(1)) * _UNIT_SECONDS[match.group(2)])


def format_duration(value: datetime.timedelta | None) -> str | None:
    """``value`` in the largest whole unit (``"30d"``, ``"90m"``, ``"45s"``)."""
    if value is None:
        return None
    seconds = int(value.total_seconds())
    for unit, size in _UNIT_SECONDS.items():
        if seconds and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _validate_count(name: str, value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        raise ValueError(f"{name} must be an integer >= 1")


@dataclass
class JobHistoryRetention:
    """How long the jobs plugin keeps job runs (global, off by default).

    A finished run is deleted when it breaks any limit that is set: older than
    ``max_age``, or not among the newest ``max_runs_per_job`` runs of its job.
    Failed (and timed-out) runs use ``failed_max_age`` /
    ``failed_max_runs_per_job`` when set (else the normal limits); with
    ``failed_max_runs_per_job`` they are counted apart from the other runs.
    Running executions and the latest run of each job are never deleted.

    With any limit set the plugin registers the system job
    ``jobs:prune-history`` (group ``system``), which prunes every
    ``prune_interval``."""

    max_age: Duration | None = None
    max_runs_per_job: int | None = None
    failed_max_age: Duration | None = None
    failed_max_runs_per_job: int | None = None
    #: How often the plugin prunes (default one day).
    prune_interval: Duration = "1d"

    def validate(self) -> None:
        """Raises ``ValueError`` for an invalid duration or count."""
        parse_duration(self.max_age)
        parse_duration(self.failed_max_age)
        interval = parse_duration(self.prune_interval)
        if not interval:
            raise ValueError("prune_interval must be a positive duration")
        _validate_count("max_runs_per_job", self.max_runs_per_job)
        _validate_count("failed_max_runs_per_job", self.failed_max_runs_per_job)

    @property
    def is_configured(self) -> bool:
        """Whether any limit is set."""
        return any(
            v is not None
            for v in (self.max_age, self.max_runs_per_job, self.failed_max_age, self.failed_max_runs_per_job)
        )


@dataclass
class CliJobHistoryOptions:
    """A job's own history retention (``CliJobOptions.history``): a field set
    here replaces the global :class:`JobHistoryRetention` one for this job;
    ``keep_all`` keeps every run of the job."""

    max_age: Duration | None = None
    max_runs_per_job: int | None = None
    failed_max_age: Duration | None = None
    failed_max_runs_per_job: int | None = None
    keep_all: bool = False

    def validate(self) -> None:
        """Raises ``ValueError`` for an invalid duration or count."""
        parse_duration(self.max_age)
        parse_duration(self.failed_max_age)
        _validate_count("max_runs_per_job", self.max_runs_per_job)
        _validate_count("failed_max_runs_per_job", self.failed_max_runs_per_job)

    @property
    def is_set(self) -> bool:
        """Whether this overrides anything (a limit set, or ``keep_all``)."""
        return self.keep_all or any(
            v is not None
            for v in (self.max_age, self.max_runs_per_job, self.failed_max_age, self.failed_max_runs_per_job)
        )


@dataclass(frozen=True)
class JobHistoryLimits:
    """The retention limits in effect for one job (global limits with the
    job's overrides applied), as passed to
    ``ICliJobStorageProvider.prune_history``."""

    max_age: datetime.timedelta | None = None
    max_runs_per_job: int | None = None
    failed_max_age: datetime.timedelta | None = None
    failed_max_runs_per_job: int | None = None
    #: The job overrides the global retention (``CliJobOptions.history``).
    overridden: bool = False
    #: The job keeps every run (no limit applies).
    keep_all: bool = False

    @staticmethod
    def resolve(
        retention: JobHistoryRetention | None,
        history: CliJobHistoryOptions | None,
    ) -> JobHistoryLimits | None:
        """The limits for a job with ``history`` under the global
        ``retention``; ``None`` when no limit applies and the job does not
        keep all its runs."""
        overridden = history is not None and history.is_set
        if overridden and history.keep_all:
            return JobHistoryLimits(overridden=True, keep_all=True)

        def pick(name: str):
            own = getattr(history, name) if overridden else None
            return own if own is not None else (getattr(retention, name) if retention else None)

        limits = JobHistoryLimits(
            max_age=parse_duration(pick("max_age")),
            max_runs_per_job=pick("max_runs_per_job"),
            failed_max_age=parse_duration(pick("failed_max_age")),
            failed_max_runs_per_job=pick("failed_max_runs_per_job"),
            overridden=overridden,
        )
        return limits if limits.has_limits else None

    @property
    def has_limits(self) -> bool:
        """Whether a run can be deleted under these limits."""
        return not self.keep_all and any(
            v is not None
            for v in (self.max_age, self.max_runs_per_job, self.failed_max_age, self.failed_max_runs_per_job)
        )

    def to_dict(self) -> dict[str, object]:
        """The job info's ``historyRetention`` (durations like ``"30d"``)."""
        return {
            "maxAge": format_duration(self.max_age),
            "maxRunsPerJob": self.max_runs_per_job,
            "failedMaxAge": format_duration(self.failed_max_age),
            "failedMaxRunsPerJob": self.failed_max_runs_per_job,
            "overridden": self.overridden,
            "keepAll": self.keep_all,
        }

    def expired_ids(
        self,
        executions: list[JobExecution],
        now: datetime.datetime,
    ) -> list[str]:
        """The ids of the runs in ``executions`` (one job's, any order) that
        break these limits, never a running one or the job's latest run.  For
        stores that select runs in memory; a database store expresses the same
        rules as set-based deletes."""
        if not self.has_limits or not executions:
            return []
        ordered = sorted(executions, key=lambda e: as_utc(e.started_at), reverse=True)
        latest_id = ordered[0].id
        separate_failed = self.failed_max_runs_per_job is not None
        rank_failed = 0
        rank_other = 0
        expired: list[str] = []
        for execution in ordered:
            failed = execution.status in FAILED_STATUSES
            if failed and separate_failed:
                rank, rank_failed = rank_failed, rank_failed + 1
                max_runs = self.failed_max_runs_per_job
            else:
                rank, rank_other = rank_other, rank_other + 1
                max_runs = self.max_runs_per_job
            max_age = self.failed_max_age if failed and self.failed_max_age is not None else self.max_age
            if execution.id == latest_id or execution.status == "running":
                continue
            too_many = max_runs is not None and rank >= max_runs
            too_old = max_age is not None and as_utc(execution.started_at) < now - max_age
            if too_many or too_old:
                expired.append(execution.id)
        return expired
