"""Background job scheduler using asyncio tasks."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Awaitable, Protocol, runtime_checkable

from croniter import croniter

from qodalis_cli_server_abstractions.jobs import (
    CliJobOptions,
    ICliJob,
    ICliJobStorageProvider,
    JobExecution,
    JobExecutionQuery,
    JobLogEntry,
    JobState,
)
from qodalis_cli_server_abstractions.jobs.job_execution_query import as_utc

from .cli_job_execution_context import CliJobExecutionContext
from .interval_parser import parse_interval

logger = logging.getLogger(__name__)

# Characters a job id may not have: it is part of the jobs API's URLs
_UNSAFE_ID_CHARS = frozenset("/\\?#")


@dataclass
class JobRegistration:
    """Runtime state of a registered job (``CliJobScheduler.jobs``)."""

    id: str
    job: ICliJob
    options: CliJobOptions
    status: str = "active"  # 'active' | 'paused' | 'stopped'
    current_execution_id: str | None = None
    current_cancellation: asyncio.Event | None = None
    next_run_at: datetime.datetime | None = None
    last_run_at: datetime.datetime | None = None
    last_run_status: str | None = None
    last_run_duration: float | None = None  # ms
    timer_task: asyncio.Task[Any] | None = None
    queue: deque[int] = field(default_factory=deque)  # queued retry_attempt values


#: Former name of :class:`JobRegistration`.
_JobRegistration = JobRegistration

BroadcastFn = Callable[[str], Awaitable[None]]


@runtime_checkable
class ICliJobScheduler(Protocol):
    """The scheduler that runs the jobs registered with the jobs plugin in this
    process: their registrations and the actions on them.  Use it from the
    host's own code (e.g. run a job when something happened):
    ``plugin.scheduler.trigger("digest")``."""

    @property
    def jobs(self) -> Mapping[str, JobRegistration]:
        """The registered jobs keyed by job id."""
        ...

    async def trigger(self, job_id: str) -> None:
        """Runs the job now; raises :class:`InvalidOperationError` when it is
        running and may not overlap, :class:`KeyError` for an unknown job."""
        ...

    async def pause(self, job_id: str) -> None:
        """Pauses the job's schedule."""
        ...

    async def resume(self, job_id: str) -> None:
        """Resumes a paused job's schedule."""
        ...

    async def stop_job(self, job_id: str) -> None:
        """Stops the job: cancels a running execution and disables its schedule."""
        ...

    async def cancel_current(self, job_id: str) -> None:
        """Cancels the job's running execution."""
        ...

    async def update_options(self, job_id: str, **changes: Any) -> None:
        """Changes the job's options and restarts its schedule."""
        ...


class CliJobScheduler:
    """Background job scheduler using asyncio tasks (implements
    :class:`ICliJobScheduler`)."""

    def __init__(
        self,
        storage: ICliJobStorageProvider,
        broadcast_fn: BroadcastFn | None = None,
    ) -> None:
        self._storage = storage
        self._broadcast_fn = broadcast_fn
        self._registrations: dict[str, JobRegistration] = {}
        self._running = False

    @property
    def registrations(self) -> dict[str, JobRegistration]:
        """Return the map of job ID to registration state."""
        return self._registrations

    @property
    def jobs(self) -> Mapping[str, JobRegistration]:
        """The registered jobs keyed by job id (read-only view)."""
        return MappingProxyType(self._registrations)

    @property
    def storage(self) -> ICliJobStorageProvider:
        """Return the storage provider used for job state persistence."""
        return self._storage

    def register(self, job: ICliJob, options: CliJobOptions) -> str:
        """Register a job. Returns the job id: ``options.id``, else the name
        (``name-2``, ``name-3``, ... for further jobs of the same name), stable
        across restarts so saved state and history find the job again.

        Raises:
            ValueError: ``options.id`` is empty or has ``/``, ``\\``, ``?`` or
                ``#`` (it is part of the jobs API's URLs; a default id replaces
                them with ``-``).
            InvalidOperationError: a job with ``options.id`` is registered.
        """
        if not options.name:
            options.name = type(job).__name__
        if not options.description:
            options.description = options.name
        if options.id is not None:
            if not options.id.strip() or any(c in options.id for c in _UNSAFE_ID_CHARS):
                raise ValueError(
                    f"Job id '{options.id}' must be non-empty and may not contain "
                    "'/', '\\', '?' or '#' (it is part of the jobs API's URLs)"
                )
            if options.id in self._registrations:
                raise InvalidOperationError(f"A job with id '{options.id}' is already registered")
            job_id = options.id
        else:
            base_id = "".join("-" if c in _UNSAFE_ID_CHARS else c for c in options.name)
            job_id = base_id
            n = 2
            while job_id in self._registrations:
                job_id = f"{base_id}-{n}"
                n += 1
        reg = JobRegistration(
            id=job_id,
            job=job,
            options=options,
            status="active" if options.enabled else "stopped",
        )
        self._registrations[job_id] = reg
        return job_id

    async def start(self) -> None:
        """Load persisted states and start timers for active jobs."""
        self._running = True
        persisted = await self._storage.get_all_job_states()

        for reg in self._registrations.values():
            # Apply persisted state if available
            # Restore the last run, and a pause or stop made at run time; a job
            # the code disables (enabled=False) stays stopped whatever was saved.
            state = persisted.get(reg.id)
            if state:
                reg.last_run_at = state.last_run_at
                if reg.options.enabled:
                    reg.status = state.status

            if reg.status == "active":
                # An interval job runs on start through its timer (due now),
                # so the two never overlap.
                run_now = bool(
                    reg.options.run_on_start and not reg.options.schedule and reg.options.interval
                )
                self._schedule_next(reg, run_now=run_now)
                if reg.options.run_on_start and not run_now:
                    asyncio.create_task(self._execute_job(reg, retry_attempt=0))

    async def stop(self) -> None:
        """Cancel all running executions, stop timers, persist states."""
        self._running = False
        for reg in list(self._registrations.values()):
            # Cancel running execution
            if reg.current_cancellation is not None:
                reg.current_cancellation.set()
            # Cancel timer
            if reg.timer_task is not None and not reg.timer_task.done():
                reg.timer_task.cancel()
                try:
                    await reg.timer_task
                except (asyncio.CancelledError, Exception):
                    pass
            # Persist state
            await self._storage.save_job_state(
                reg.id,
                JobState(
                    status=reg.status,
                    last_run_at=reg.last_run_at,
                    updated_at=datetime.datetime.now(datetime.UTC),
                ),
            )

    async def trigger(self, job_id: str) -> None:
        """Manually trigger a job execution."""
        reg = self._get_registration(job_id)
        if reg.current_execution_id is not None:
            policy = reg.options.overlap_policy
            if policy == "skip":
                raise InvalidOperationError("Job is already running")
            elif policy == "cancel":
                if reg.current_cancellation:
                    reg.current_cancellation.set()
            elif policy == "queue":
                reg.queue.append(0)
                return
        asyncio.create_task(self._execute_job(reg, retry_attempt=0))

    async def pause(self, job_id: str) -> None:
        """Pause a running job, cancelling its next scheduled execution."""
        reg = self._get_registration(job_id)
        if reg.status == "paused":
            raise InvalidOperationError("Job is already paused")
        reg.status = "paused"
        self._cancel_timer(reg)
        await self._persist_state(reg)
        await self._broadcast({"type": "job:paused", "jobId": job_id})

    async def resume(self, job_id: str) -> None:
        """Resume a paused job and reschedule its next execution."""
        reg = self._get_registration(job_id)
        if reg.status != "paused":
            raise InvalidOperationError("Job is not paused")
        reg.status = "active"
        self._schedule_next(reg)
        await self._persist_state(reg)
        await self._broadcast({"type": "job:resumed", "jobId": job_id})

    async def stop_job(self, job_id: str) -> None:
        """Stop a job permanently, cancelling any running execution."""
        reg = self._get_registration(job_id)
        reg.status = "stopped"
        self._cancel_timer(reg)
        if reg.current_cancellation is not None:
            reg.current_cancellation.set()
        await self._persist_state(reg)
        await self._broadcast({"type": "job:stopped", "jobId": job_id})

    async def cancel_current(self, job_id: str) -> None:
        """Cancel the currently running execution for a job."""
        reg = self._get_registration(job_id)
        if reg.current_execution_id is None or reg.current_cancellation is None:
            raise InvalidOperationError("No execution is currently running")
        reg.current_cancellation.set()

    async def update_options(
        self,
        job_id: str,
        *,
        description: str | None = None,
        group: str | None = None,
        schedule: str | None = None,
        interval: str | None = None,
        max_retries: int | None = None,
        retry_delay: str | None = None,
        retry_strategy: str | None = None,
        timeout: str | None = None,
        overlap_policy: str | None = None,
    ) -> None:
        """Update mutable options for a registered job and reschedule if active."""
        reg = self._get_registration(job_id)
        opts = reg.options

        if description is not None:
            opts.description = description
        if group is not None:
            opts.group = group
        if schedule is not None and interval is not None:
            raise ValueError("Cannot provide both schedule and interval")
        if schedule is not None:
            if not croniter.is_valid(schedule):
                raise ValueError(f"Invalid cron expression: {schedule!r}")
            opts.schedule = schedule
            opts.interval = None
        if interval is not None:
            parse_interval(interval)  # validate
            opts.interval = interval
            opts.schedule = None
        if max_retries is not None:
            opts.max_retries = max_retries
        if retry_delay is not None:
            parse_interval(retry_delay)  # validate
            opts.retry_delay = retry_delay
        if retry_strategy is not None:
            if retry_strategy not in ("fixed", "linear", "exponential"):
                raise ValueError(f"Invalid retry strategy: {retry_strategy!r}")
            opts.retry_strategy = retry_strategy
        if timeout is not None:
            parse_interval(timeout)  # validate
            opts.timeout = timeout
        if overlap_policy is not None:
            if overlap_policy not in ("skip", "queue", "cancel"):
                raise ValueError(f"Invalid overlap policy: {overlap_policy!r}")
            opts.overlap_policy = overlap_policy

        # Reschedule if active
        if reg.status == "active":
            self._cancel_timer(reg)
            self._schedule_next(reg)

    async def query_executions(
        self, query: JobExecutionQuery
    ) -> tuple[list[JobExecution], int]:
        """Executions of several jobs (or of every job), newest first.  Uses
        the storage's own query; a storage that cannot list every job gets the
        registered jobs' ids merged per job."""
        try:
            return await self._storage.query_executions(query)
        except NotImplementedError:
            if query.job_ids is not None:
                raise

        async def list_job(job_id: str, limit: int, status: str | None):
            return await self._storage.get_executions(job_id, limit=limit, offset=0, status=status)

        return await query.merge(list(self._registrations), list_job)

    def _schedule_next(
        self, reg: JobRegistration, *, run_now: bool = False, after_fire: bool = False
    ) -> None:
        """Compute next run time and create timer task.

        An interval job keeps its rhythm across restarts and pauses: it is due
        one interval after its last run (right away when that has passed, or
        with ``run_now``); right after a firing, one interval from now."""
        if not self._running:
            return

        now = datetime.datetime.now(datetime.UTC)
        delay: float

        if reg.options.schedule:
            cron = croniter(reg.options.schedule, now)
            next_dt = cron.get_next(datetime.datetime)
            if next_dt.tzinfo is None:
                next_dt = next_dt.replace(tzinfo=datetime.UTC)
            reg.next_run_at = next_dt
            delay = max((next_dt - now).total_seconds(), 0.1)
        elif reg.options.interval:
            interval = datetime.timedelta(seconds=parse_interval(reg.options.interval))
            if reg.last_run_at is not None and not after_fire:
                due = as_utc(reg.last_run_at) + interval
            else:
                due = now + interval
            if due < now or run_now:
                due = now
            reg.next_run_at = due
            delay = (due - now).total_seconds()
        else:
            # No schedule configured — job can only be triggered manually
            reg.next_run_at = None
            return

        reg.timer_task = asyncio.create_task(self._timer_fire(reg, delay))

    async def _timer_fire(self, reg: JobRegistration, delay: float) -> None:
        """Wait for the delay, then execute the job and reschedule."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        if not self._running or reg.status != "active":
            return

        # Overlap check
        if reg.current_execution_id is not None:
            policy = reg.options.overlap_policy
            if policy == "skip":
                pass  # skip this firing
            elif policy == "cancel":
                if reg.current_cancellation:
                    reg.current_cancellation.set()
                asyncio.create_task(self._execute_job(reg, retry_attempt=0))
            elif policy == "queue":
                reg.queue.append(0)
        else:
            asyncio.create_task(self._execute_job(reg, retry_attempt=0))

        # Reschedule
        if reg.status == "active":
            self._schedule_next(reg, after_fire=True)

    async def _execute_job(self, reg: JobRegistration, retry_attempt: int) -> None:
        exec_id = str(uuid.uuid4())
        cancel_event = asyncio.Event()
        now = datetime.datetime.now(datetime.UTC)

        reg.current_execution_id = exec_id
        reg.current_cancellation = cancel_event

        execution = JobExecution(
            id=exec_id,
            job_id=reg.id,
            job_name=reg.options.name or type(reg.job).__name__,
            status="running",
            started_at=now,
            retry_attempt=retry_attempt,
        )
        await self._storage.save_execution(execution)

        await self._broadcast({
            "type": "job:started",
            "jobId": reg.id,
            "executionId": exec_id,
            "timestamp": now.isoformat(),
        })

        ctx = CliJobExecutionContext()

        try:
            timeout_seconds: float | None = None
            if reg.options.timeout:
                timeout_seconds = parse_interval(reg.options.timeout)

            if timeout_seconds:
                await asyncio.wait_for(
                    reg.job.execute_async(ctx, cancel_event),
                    timeout=timeout_seconds,
                )
            else:
                await reg.job.execute_async(ctx, cancel_event)

            # Check if cancellation was requested (cooperative)
            if cancel_event.is_set():
                execution.status = "cancelled"
                await self._broadcast({
                    "type": "job:cancelled",
                    "jobId": reg.id,
                    "executionId": exec_id,
                })
            else:
                execution.status = "completed"

        except asyncio.TimeoutError:
            cancel_event.set()
            execution.status = "timed_out"
            execution.error = f"Timed out after {reg.options.timeout}"
            await self._broadcast({
                "type": "job:timed_out",
                "jobId": reg.id,
                "executionId": exec_id,
                "timeout": reg.options.timeout,
            })
        except asyncio.CancelledError:
            execution.status = "cancelled"
            await self._broadcast({
                "type": "job:cancelled",
                "jobId": reg.id,
                "executionId": exec_id,
            })
        except Exception as exc:
            execution.status = "failed"
            execution.error = str(exc)
            await self._broadcast({
                "type": "job:failed",
                "jobId": reg.id,
                "executionId": exec_id,
                "error": str(exc),
            })

        # Finalize
        end_time = datetime.datetime.now(datetime.UTC)
        execution.completed_at = end_time
        execution.duration = round((end_time - execution.started_at).total_seconds() * 1000)
        execution.logs = list(ctx.log_entries)
        execution.properties = dict(ctx.properties)
        # A completed run the job discarded is left out of the history (a
        # store that cannot delete keeps it); failed runs are always kept.
        await self._save_finished(execution, ctx.discarded)

        reg.last_run_at = execution.started_at
        reg.last_run_status = execution.status
        reg.last_run_duration = execution.duration
        reg.current_execution_id = None
        reg.current_cancellation = None
        # The last run survives a restart (interval rhythm, see _schedule_next)
        try:
            await self._persist_state(reg)
        except Exception:
            logger.exception("Saving the state of job %s failed", reg.id)

        if execution.status == "completed":
            await self._broadcast({
                "type": "job:completed",
                "jobId": reg.id,
                "executionId": exec_id,
                "duration": execution.duration,
            })

        # Retry on failure
        if execution.status == "failed" and retry_attempt < reg.options.max_retries:
            base_delay = parse_interval(reg.options.retry_delay) if reg.options.retry_delay else 5.0
            strategy = reg.options.retry_strategy or "exponential"
            if strategy == "fixed":
                delay = base_delay
            elif strategy == "linear":
                delay = base_delay * (retry_attempt + 1)
            else:  # exponential
                delay = base_delay * (2 ** retry_attempt)
            logger.info(
                "Retrying job %s in %.1fs (attempt %d/%d)",
                reg.options.name,
                delay,
                retry_attempt + 1,
                reg.options.max_retries,
            )
            await self._broadcast({
                "type": "job:retrying",
                "jobId": reg.id,
                "executionId": exec_id,
                "attempt": retry_attempt + 1,
                "maxRetries": reg.options.max_retries,
                "delayMs": round(delay * 1000),
            })
            await asyncio.sleep(delay)
            asyncio.create_task(self._execute_job(reg, retry_attempt + 1))
            return

        # Process queue
        if reg.queue:
            queued_attempt = reg.queue.popleft()
            asyncio.create_task(self._execute_job(reg, queued_attempt))

    async def _save_finished(self, execution: JobExecution, discard: bool) -> None:
        """Save the finished run, or delete a discarded one (a store that
        cannot delete keeps it; failed runs are always kept). When the store
        refuses the run's properties (a value that does not serialize), the run
        is saved without them, so it never stays "running"."""
        try:
            discarded = (
                discard
                and execution.status == "completed"
                and await self._storage.delete_execution(execution.id)
            )
            if not discarded:
                await self._storage.save_execution(execution)
        except Exception as exc:
            if not execution.properties:
                raise
            logger.exception(
                "Saving execution %s of job %s failed; saving it without its properties",
                execution.id,
                execution.job_id,
            )
            execution.properties = {}
            execution.logs.append(JobLogEntry(
                level="error",
                message=f"The run's properties could not be saved: {exc}",
            ))
            await self._storage.save_execution(execution)

    def _get_registration(self, job_id: str) -> JobRegistration:
        reg = self._registrations.get(job_id)
        if reg is None:
            raise KeyError(f"Job not found: {job_id}")
        return reg

    def _cancel_timer(self, reg: JobRegistration) -> None:
        if reg.timer_task is not None and not reg.timer_task.done():
            reg.timer_task.cancel()
        reg.timer_task = None
        reg.next_run_at = None

    async def _persist_state(self, reg: JobRegistration) -> None:
        await self._storage.save_job_state(
            reg.id,
            JobState(
                status=reg.status,
                last_run_at=reg.last_run_at,
                updated_at=datetime.datetime.now(datetime.UTC),
            ),
        )

    async def _broadcast(self, message: dict[str, Any]) -> None:
        if self._broadcast_fn is not None:
            try:
                await self._broadcast_fn(json.dumps(message))
            except Exception:
                logger.debug("Failed to broadcast job event", exc_info=True)


class InvalidOperationError(Exception):
    """Raised when a job control operation is not valid in the current state."""
