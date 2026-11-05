"""Run statistics of the jobs (``GET /api/v1/qcli/jobs/stats``) for the
admin dashboard's charts: runs per time column by outcome, totals and per-job
figures, computed from every run started in the range."""

from __future__ import annotations

import datetime
import math
from typing import Any, Awaitable, Callable

from qodalis_cli_server_abstractions.jobs import JobExecution, JobExecutionQuery

DEFAULT_BUCKETS = 24
MAX_BUCKETS = 200
DEFAULT_RANGE = datetime.timedelta(hours=24)
#: Executions read per page of the execution query.
PAGE_SIZE = 500
#: Runs listed per job in ``recent``.
RECENT_RUNS = 12

OUTCOMES = ("succeeded", "failed", "cancelled", "running")

_SUCCEEDED = {"completed", "succeeded"}
_FAILED = {"failed", "error", "timed_out", "timedout"}
_RUNNING = {"running", "pending"}

QueryExecutions = Callable[[JobExecutionQuery], Awaitable["tuple[list[JobExecution], int]"]]


class StatsRequestError(ValueError):
    """A bad query value (answered with 400)."""


def outcome_of(status: str | None) -> str:
    """The chart outcome of a run status."""
    value = (status or "").strip().lower()
    if value in _SUCCEEDED:
        return "succeeded"
    if value in _FAILED:
        return "failed"
    if value in _RUNNING:
        return "running"
    return "cancelled"


def parse_time(value: str | None, name: str) -> datetime.datetime | None:
    """An ISO 8601 query time as aware UTC (no offset = UTC); ``None`` when
    absent; :class:`StatsRequestError` when not a time."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise StatsRequestError(f"'{name}' must be an ISO 8601 time, got '{value}'") from None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def parse_buckets(value: str | None) -> int:
    """``buckets`` clamped to 1..200 (default 24)."""
    if value is None or not value.strip():
        return DEFAULT_BUCKETS
    try:
        count = int(value.strip())
    except ValueError:
        raise StatsRequestError(f"'buckets' must be an integer, got '{value}'") from None
    return min(max(count, 1), MAX_BUCKETS)


def resolve_range(
    since: str | None, until: str | None, *, now: datetime.datetime | None = None,
) -> tuple[datetime.datetime, datetime.datetime]:
    """``[since, until)``: until defaults to now, since to until - 24h."""
    end = parse_time(until, "until") or (now or datetime.datetime.now(datetime.timezone.utc))
    start = parse_time(since, "since") or end - DEFAULT_RANGE
    if start >= end:
        raise StatsRequestError("'since' must be before 'until'")
    return start, end


def iso(value: datetime.datetime | None) -> str | None:
    """``2026-11-05T21:00:00.000Z`` (UTC, milliseconds)."""
    if value is None:
        return None
    utc = value.astimezone(datetime.timezone.utc) if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _utc(value: datetime.datetime) -> datetime.datetime:
    return value.replace(tzinfo=datetime.timezone.utc) if value.tzinfo is None else value.astimezone(datetime.timezone.utc)


def _number(value: float) -> float | int:
    """Whole numbers as ints, so ``bucketMs`` reads 3600000, not 3600000.0."""
    return int(value) if float(value).is_integer() else value


async def collect_runs(
    query_executions: QueryExecutions,
    job_ids: list[str] | None,
    since: datetime.datetime,
    until: datetime.datetime,
) -> list[JobExecution]:
    """Every run started in ``[since, until)``: pages through the execution
    query until it is exhausted (no cap), so any storage provider works."""
    runs: dict[str, JobExecution] = {}
    offset = 0
    while True:
        items, total = await query_executions(JobExecutionQuery(
            job_ids=job_ids, since=since, limit=PAGE_SIZE, offset=offset,
        ))
        for execution in items:
            started = _utc(execution.started_at)
            if since <= started < until:
                runs.setdefault(execution.id or f"#{id(execution)}", execution)
        offset += len(items)
        if not items or len(items) < PAGE_SIZE or offset >= total:
            break
    return list(runs.values())


def _summary(runs: list[JobExecution]) -> dict[str, Any]:
    """Counts by outcome, success rate and duration figures of some runs."""
    counts = {outcome: 0 for outcome in OUTCOMES}
    durations: list[float] = []
    last_failure: datetime.datetime | None = None
    for run in runs:
        outcome = outcome_of(run.status)
        counts[outcome] += 1
        if outcome != "running" and run.duration is not None:
            durations.append(float(run.duration))
        if outcome == "failed":
            started = _utc(run.started_at)
            if last_failure is None or started > last_failure:
                last_failure = started
    decided = counts["succeeded"] + counts["failed"]
    durations.sort()
    return {
        "total": len(runs),
        **counts,
        "successRate": counts["succeeded"] / decided if decided else None,
        "avgDuration": sum(durations) / len(durations) if durations else None,
        # Nearest rank: sorted[ceil(0.95 * n) - 1].
        "p95Duration": durations[math.ceil(0.95 * len(durations)) - 1] if durations else None,
        "maxDuration": durations[-1] if durations else None,
        "lastFailureAt": iso(last_failure),
    }


def compute_stats(
    runs: list[JobExecution],
    since: datetime.datetime,
    until: datetime.datetime,
    buckets: int,
    job_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The ``GET /jobs/stats`` body for ``runs`` (all started in the range)."""
    since_ms = since.timestamp() * 1000
    bucket_ms = (until.timestamp() * 1000 - since_ms) / buckets
    columns = [
        {
            "start": iso(since + datetime.timedelta(milliseconds=i * bucket_ms)),
            **{outcome: 0 for outcome in OUTCOMES},
        }
        for i in range(buckets)
    ]
    by_job: dict[str, list[JobExecution]] = {}
    for run in runs:
        offset = _utc(run.started_at).timestamp() * 1000 - since_ms
        index = min(max(int(offset // bucket_ms), 0), buckets - 1)
        columns[index][outcome_of(run.status)] += 1
        by_job.setdefault(run.job_id, []).append(run)

    jobs = []
    for job_id in sorted(by_job):
        job_runs = sorted(by_job[job_id], key=lambda r: _utc(r.started_at), reverse=True)
        name = next((r.job_name for r in job_runs if r.job_name), None)
        jobs.append({
            "jobId": job_id,
            "jobName": name or (job_names or {}).get(job_id) or job_id,
            **_summary(job_runs),
            "recent": [
                {"status": r.status, "startedAt": iso(r.started_at), "duration": r.duration}
                for r in job_runs[:RECENT_RUNS]
            ],
        })

    return {
        "since": iso(since),
        "until": iso(until),
        "bucketMs": _number(bucket_ms),
        "buckets": columns,
        "totals": _summary(runs),
        "jobs": jobs,
    }
