"""Background job scheduler using asyncio tasks."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import uuid
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
from qodalis_cli_server_abstractions.events import CliEventAudience, ICliEventBus

from .cli_job_execution_context import CliJobExecutionContext
from .interval_parser import parse_interval

logger = logging.getLogger(__name__)

#: Permission of the audience of every ``job:*`` event.
JOBS_READ = "jobs:read"

# Characters a job id may not have: it is part of the jobs API's URLs
_UNSAFE_ID_CHARS = frozenset("/\\?#")


#: Error of a run left ``running`` by a previous process (finished at start).
INTERRUPTED_ERROR = "Interrupted: the server stopped during the run"


@dataclass(eq=False)
class JobRun:
    """One run of a job.  It holds one of the job's ``max_concurrent_runs``
    slots from its start until it ends, its retries and the delays between
    them included."""

    #: Set to cancel the run (its current attempt and any retry to come).
    cancellation: asyncio.Event = field(default_factory=asyncio.Event)
    #: The execution of the running attempt (``None`` between attempts).
    execution_id: str | None = None
    task: asyncio.Task[Any] | None = None


@dataclass
class JobRegistration:
    """Runtime state of a registered job (``CliJobScheduler.jobs``)."""

    id: str
    job: ICliJob
    options: CliJobOptions
    status: str = "active"  # 'active' | 'paused' | 'stopped'
    next_run_at: datetime.datetime | None = None
    last_run_at: datetime.datetime | None = None
    last_run_status: str | None = None
    last_run_duration: float | None = None  # ms
    timer_task: asyncio.Task[Any] | None = None
    #: The runs holding a slot, oldest first.
    runs: list[JobRun] = field(default_factory=list)
    #: A run waits for a free slot (``queue`` overlap policy; at most one).
    queued: bool = False

    @property
    def running_execution_ids(self) -> list[str]:
        """Executions of the running attempts, oldest run first."""
        return [r.execution_id for r in self.runs if r.execution_id is not None]

    @property
    def current_execution_id(self) -> str | None:
        """The newest running execution, or ``None``."""
        ids = self.running_execution_ids
        return ids[-1] if ids else None

    @property
    def current_cancellation(self) -> asyncio.Event | None:
        """The newest run's cancellation event, or ``None`` when idle."""
        return self.runs[-1].cancellation if self.runs else None


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

    async def trigger(self, job_id: str) -> Any:
        """Runs the job now (or queues the run, ``queue`` policy, when every
        slot is taken); raises :class:`InvalidOperationError` when every slot
        is taken and the policy is ``skip``, :class:`KeyError` for an unknown
        job."""
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
        """Cancels every running run of the job and drops a queued one."""
        ...

    async def update_options(self, job_id: str, **changes: Any) -> None:
        """Changes the job's options and restarts its schedule."""
        ...

    async def delete_execution(self, job_id: str, execution_id: str) -> None:
        """Deletes one finished run of the job and its logs; raises
        :class:`KeyError` for an unknown job, :class:`ExecutionNotFoundError`
        for a run that is missing or belongs to another job,
        :class:`InvalidOperationError` for a running run and
        :class:`NotSupportedError` when the storage cannot delete runs."""
        ...

    async def delete_executions(self, job_id: str, status: str | None = None) -> int:
        """Deletes every finished run of the job (only those with ``status``
        when given) and returns how many; running runs are kept."""
        ...


class CliJobScheduler:
    """Background job scheduler using asyncio tasks (implements
    :class:`ICliJobScheduler`).

    Its ``job:*`` events go to ``event_bus`` (audience ``jobs:read``); without
    a bus, to ``broadcast_fn`` as flat JSON (every socket).  Mounting the jobs
    plugin sets the server's bus."""

    def __init__(
        self,
        storage: ICliJobStorageProvider,
        broadcast_fn: BroadcastFn | None = None,
        event_bus: ICliEventBus | None = None,
    ) -> None:
        self._storage = storage
        self._broadcast_fn = broadcast_fn
        #: The server's event bus (``job:*`` events, audience ``jobs:read``).
        self.event_bus = event_bus
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
        _validate_max_concurrent_runs(options.max_concurrent_runs)
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
        """Finish runs a previous process left running, load persisted states
        and start timers for active jobs."""
        self._running = True
        await self._finish_interrupted_runs()
        persisted = await self._storage.get_all_job_states()
        now = datetime.datetime.now(datetime.UTC)

        for reg in self._registrations.values():
            # Apply persisted state if available
            # Restore the last run, and a pause or stop made at run time; a job
            # the code disables (enabled=False) stays stopped whatever was saved.
            state = persisted.get(reg.id)
            if state:
                reg.last_run_at = state.last_run_at
                if reg.options.enabled:
                    reg.status = state.status

            if reg.status != "active":
                continue
            run_on_start = reg.options.run_on_start and self._due_on_start(reg, now)
            if reg.options.interval and not reg.options.schedule:
                # The timer runs it (due now), so the two never overlap.
                self._schedule_next(reg, run_now=run_on_start)
            else:
                self._schedule_next(reg)
                if run_on_start:
                    self._launch(reg, self._admit(reg))

    def _due_on_start(self, reg: JobRegistration, now: datetime.datetime) -> bool:
        """Run on start unless the job ran recently: never ran, its interval
        has passed, or a cron occurrence was missed; always for a job without
        a schedule."""
        last = reg.last_run_at
        if reg.options.schedule:
            if last is None:
                return True
            return _next_occurrence(reg.options.schedule, as_utc(last)) <= now
        if reg.options.interval:
            if last is None:
                return True
            return as_utc(last) + datetime.timedelta(seconds=parse_interval(reg.options.interval)) <= now
        return True

    async def _finish_interrupted_runs(self) -> None:
        """Runs of the registered jobs that the store still has as running
        were cut off by a previous process: finish them as cancelled.  No
        events (no client is connected yet)."""
        for reg in list(self._registrations.values()):
            try:
                stale: list[JobExecution] = []
                offset = 0
                while True:
                    items, total = await self._storage.get_executions(
                        reg.id, limit=100, offset=offset, status="running"
                    )
                    stale.extend(e for e in items if e.status == "running")
                    offset += len(items)
                    if not items or offset >= total:
                        break
                ours = set(reg.running_execution_ids)
                for execution in stale:
                    if execution.id in ours:
                        continue
                    started = as_utc(execution.started_at)
                    # The last thing it logged is the latest time it is known
                    # to have run; else its start (durations stay sane).
                    ended = max(
                        [started, *(as_utc(e.timestamp) for e in execution.logs)],
                    )
                    execution.status = "cancelled"
                    execution.error = INTERRUPTED_ERROR
                    execution.completed_at = ended
                    execution.duration = round((ended - started).total_seconds() * 1000)
                    await self._storage.save_execution(execution)
            except Exception:
                logger.exception("Finishing the interrupted runs of job %s failed", reg.id)

    async def stop(self) -> None:
        """Cancel all running executions, stop timers, persist states."""
        self._running = False
        for reg in list(self._registrations.values()):
            # Cancel running runs (and a queued one)
            self._cancel_runs(reg)
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

    async def trigger(self, job_id: str) -> str:
        """Manually trigger a run: ``"started"``, or ``"queued"`` when every
        slot is taken and the overlap policy is ``queue`` (or ``cancel``: the
        run starts once the cancelled oldest run has ended).

        Raises:
            KeyError: unknown job.
            InvalidOperationError: every slot is taken (``skip`` policy).
        """
        reg = self._get_registration(job_id)
        run = self._admit(reg, manual=True)
        # Published first, so job:triggered comes before the run's job:started.
        await self._broadcast({"type": "job:triggered", "jobId": job_id})
        self._launch(reg, run)
        return "started" if run is not None else "queued"

    async def pause(self, job_id: str) -> None:
        """Pause a running job, cancelling its next scheduled execution."""
        reg = self._get_registration(job_id)
        if reg.status == "paused":
            raise InvalidOperationError("Job is already paused")
        reg.status = "paused"
        self._cancel_timer(reg)
        await self._persist_state(reg)
        await self._broadcast({"type": "job:paused", "jobId": job_id, "status": "paused"})

    async def resume(self, job_id: str) -> None:
        """Resume a paused job and reschedule its next execution."""
        reg = self._get_registration(job_id)
        if reg.status != "paused":
            raise InvalidOperationError("Job is not paused")
        reg.status = "active"
        self._schedule_next(reg)
        await self._persist_state(reg)
        await self._broadcast({"type": "job:resumed", "jobId": job_id, "status": "active"})

    async def stop_job(self, job_id: str) -> None:
        """Stop a job permanently, cancelling its runs and a queued one."""
        reg = self._get_registration(job_id)
        reg.status = "stopped"
        self._cancel_timer(reg)
        self._cancel_runs(reg)
        await self._persist_state(reg)
        await self._broadcast({"type": "job:stopped", "jobId": job_id, "status": "stopped"})

    async def cancel_current(self, job_id: str) -> None:
        """Cancel every running run of the job (retries to come included) and
        drop a queued one."""
        reg = self._get_registration(job_id)
        if not reg.runs and not reg.queued:
            raise InvalidOperationError("No execution is currently running")
        self._cancel_runs(reg)

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
        max_concurrent_runs: int | None = None,
    ) -> None:
        """Update mutable options for a registered job and reschedule if active.

        Lowering ``max_concurrent_runs`` below the running count cancels
        nothing: new runs start once fewer are running."""
        reg = self._get_registration(job_id)
        opts = reg.options
        if max_concurrent_runs is not None:
            _validate_max_concurrent_runs(max_concurrent_runs)

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
        if max_concurrent_runs is not None:
            opts.max_concurrent_runs = max_concurrent_runs
            # A higher limit may free a slot for a queued run.
            self._start_queued(reg)

        # Reschedule if active
        if reg.status == "active":
            self._cancel_timer(reg)
            self._schedule_next(reg)
        await self._broadcast({"type": "job:updated", "jobId": job_id})

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

    async def delete_execution(self, job_id: str, execution_id: str) -> None:
        """Deletes one finished run of the job and its logs.

        Raises:
            KeyError: the job is not registered.
            ExecutionNotFoundError: the run is missing or belongs to another job.
            InvalidOperationError: the run is still running.
            NotSupportedError: the storage cannot delete runs.
        """
        self._get_registration(job_id)
        self._ensure_storage_deletes()
        execution = await self._storage.get_execution(execution_id)
        if execution is None or execution.job_id != job_id:
            raise ExecutionNotFoundError(f"Execution not found: {execution_id}")
        if self._is_running(execution):
            raise InvalidOperationError("The execution is still running")
        if not await self._storage.delete_execution(execution_id):
            raise ExecutionNotFoundError(f"Execution not found: {execution_id}")
        await self._broadcast({"type": "job:runs-deleted", "jobId": job_id, "executionId": execution_id})

    async def delete_executions(self, job_id: str, status: str | None = None) -> int:
        """Deletes every finished run of the job, or only those with ``status``
        (matched like the history's status filter); returns how many.  Running
        runs are kept.  Raises :class:`KeyError` for an unknown job and
        :class:`NotSupportedError` when the storage cannot delete runs."""
        self._get_registration(job_id)
        self._ensure_storage_deletes()
        page_size = 100
        to_delete: list[str] = []
        offset = 0
        while True:
            items, total = await self._storage.get_executions(
                job_id, limit=page_size, offset=offset, status=status or None
            )
            to_delete.extend(e.id for e in items if not self._is_running(e))
            offset += len(items)
            if not items or offset >= total:
                break
        deleted = 0
        for execution_id in to_delete:
            if await self._storage.delete_execution(execution_id):
                deleted += 1
        if deleted:
            await self._broadcast({"type": "job:runs-deleted", "jobId": job_id, "count": deleted})
        return deleted

    def _is_running(self, execution: JobExecution) -> bool:
        reg = self._registrations.get(execution.job_id)
        return execution.status == "running" or (
            reg is not None and execution.id in reg.running_execution_ids
        )

    def _ensure_storage_deletes(self) -> None:
        """Raises :class:`NotSupportedError` when the storage keeps the base
        ``delete_execution`` (which deletes nothing)."""
        if type(self._storage).delete_execution is ICliJobStorageProvider.delete_execution:
            raise NotSupportedError("The job storage cannot delete executions")

    def _schedule_next(
        self,
        reg: JobRegistration,
        *,
        run_now: bool = False,
        after_fire: bool = False,
        fired_at: datetime.datetime | None = None,
    ) -> None:
        """Compute next run time and create timer task.

        An interval job keeps its rhythm across restarts and pauses: it is due
        one interval after its last run (right away when that has passed, or
        with ``run_now``); right after a firing, one interval from now.  A
        cron job's next run follows the occurrence that fired (``fired_at``),
        so a timer that fires early never runs one occurrence twice."""
        if not self._running:
            return

        now = datetime.datetime.now(datetime.UTC)
        delay: float

        if reg.options.schedule:
            next_dt = None
            if fired_at is not None:
                next_dt = _next_occurrence(reg.options.schedule, as_utc(fired_at))
                if next_dt <= now:  # busy, or the clock jumped
                    next_dt = None
            if next_dt is None:
                next_dt = _next_occurrence(reg.options.schedule, now)
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
        """Wait for the delay, then start a run (through the slots) and reschedule."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        if not self._running or reg.status != "active":
            return

        fired_at = reg.next_run_at
        self._launch(reg, self._admit(reg))

        # Reschedule
        if reg.status == "active":
            self._schedule_next(reg, after_fire=True, fired_at=fired_at)

    # ------------------------------------------------------------------
    # Slots: a run holds one of max_concurrent_runs from start to end
    # ------------------------------------------------------------------

    def _admit(self, reg: JobRegistration, *, manual: bool = False) -> JobRun | None:
        """Decide a due run (timer, trigger, run on start) and take its slot.

        Synchronous on purpose: nothing else runs on the event loop between
        the check and taking the slot, so due runs at the same moment never
        exceed the limit.  Returns the run to :meth:`_launch`, or ``None``
        when it was queued or skipped (a manual trigger is refused instead).

        ``cancel`` cancels the oldest run and queues the new one: it starts
        once the cancelled run has ended, so the limit holds even for a job
        that ignores its cancellation.  Due runs while it waits merge into it
        (no other run is cancelled)."""
        limit = reg.options.max_concurrent_runs
        if len(reg.runs) < limit:
            return self._take_slot(reg)
        policy = reg.options.overlap_policy
        if policy == "queue":
            reg.queued = True  # coalesces: at most one waits
            return None
        if policy == "cancel":
            if not reg.queued:
                reg.runs[0].cancellation.set()
                reg.queued = True
            return None
        if manual:
            raise InvalidOperationError("Job is already running")
        return None

    @staticmethod
    def _take_slot(reg: JobRegistration) -> JobRun:
        run = JobRun()
        reg.runs.append(run)
        return run

    def _launch(self, reg: JobRegistration, run: JobRun | None) -> None:
        if run is not None:
            run.task = asyncio.ensure_future(self._run(reg, run))

    def _release(self, reg: JobRegistration, run: JobRun) -> None:
        if run in reg.runs:
            reg.runs.remove(run)
        self._start_queued(reg)

    def _start_queued(self, reg: JobRegistration) -> None:
        if reg.queued and len(reg.runs) < reg.options.max_concurrent_runs:
            reg.queued = False
            self._launch(reg, self._take_slot(reg))

    @staticmethod
    def _cancel_runs(reg: JobRegistration) -> None:
        reg.queued = False
        for run in reg.runs:
            run.cancellation.set()

    async def _run(self, reg: JobRegistration, run: JobRun) -> None:
        """One run: the attempt, and its retries while it fails; the slot is
        held until the end, the delays between attempts included."""
        attempt = 0
        try:
            while True:
                execution = await self._execute_attempt(reg, run, attempt)
                if (
                    execution.status != "failed"
                    or attempt >= reg.options.max_retries
                    or run.cancellation.is_set()
                ):
                    return
                delay = self._retry_delay(reg, attempt)
                logger.info(
                    "Retrying job %s in %.1fs (attempt %d/%d)",
                    reg.options.name,
                    delay,
                    attempt + 1,
                    reg.options.max_retries,
                )
                await self._broadcast({
                    "type": "job:retrying",
                    "jobId": reg.id,
                    "executionId": execution.id,
                    "attempt": attempt + 1,
                    "maxRetries": reg.options.max_retries,
                    "delayMs": round(delay * 1000),
                })
                try:
                    await asyncio.wait_for(run.cancellation.wait(), timeout=delay)
                    return  # cancelled while waiting for the retry
                except asyncio.TimeoutError:
                    pass
                attempt += 1
        except Exception:
            logger.exception("Run of job %s failed", reg.id)
        finally:
            self._release(reg, run)

    @staticmethod
    def _retry_delay(reg: JobRegistration, retry_attempt: int) -> float:
        base_delay = parse_interval(reg.options.retry_delay) if reg.options.retry_delay else 5.0
        strategy = reg.options.retry_strategy or "exponential"
        if strategy == "fixed":
            return base_delay
        if strategy == "linear":
            return base_delay * (retry_attempt + 1)
        return base_delay * (2 ** retry_attempt)  # exponential

    async def _execute_attempt(
        self, reg: JobRegistration, run: JobRun, retry_attempt: int,
    ) -> JobExecution:
        exec_id = str(uuid.uuid4())
        cancel_event = run.cancellation
        now = datetime.datetime.now(datetime.UTC)

        run.execution_id = exec_id

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
            if cancel_event.is_set():
                # Cancelled by hand (cancel, stop, overlap cancel) before the
                # timeout ran out: a job that ignores its cancellation is
                # still a cancelled run, not a timed-out one.
                execution.status = "cancelled"
                await self._broadcast({
                    "type": "job:cancelled",
                    "jobId": reg.id,
                    "executionId": exec_id,
                })
            else:
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
        run.execution_id = None
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

        return execution

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
        """Publish a ``job:*`` event (``message``: ``type`` and the fields)."""
        data = dict(message)
        event_type = data.pop("type")
        try:
            if self.event_bus is not None:
                await self.event_bus.publish(event_type, data, CliEventAudience(permission=JOBS_READ))
            elif self._broadcast_fn is not None:
                data.setdefault("timestamp", datetime.datetime.now(datetime.UTC).isoformat())
                await self._broadcast_fn(json.dumps({"type": event_type, **data}))
        except Exception:
            logger.debug("Failed to publish job event", exc_info=True)


def _next_occurrence(schedule: str, after: datetime.datetime) -> datetime.datetime:
    """The cron ``schedule``'s first occurrence after ``after`` (UTC)."""
    next_dt = croniter(schedule, after).get_next(datetime.datetime)
    if next_dt.tzinfo is None:
        next_dt = next_dt.replace(tzinfo=datetime.UTC)
    return next_dt


def _validate_max_concurrent_runs(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidValueError("maxConcurrentRuns must be an integer >= 1")


class InvalidValueError(ValueError):
    """An option value out of range (the jobs API answers 400 ``INVALID_VALUE``)."""


class InvalidOperationError(Exception):
    """Raised when a job control operation is not valid in the current state."""


class ExecutionNotFoundError(LookupError):
    """Raised when an execution is missing or belongs to another job."""


class NotSupportedError(Exception):
    """Raised when the job storage cannot perform an operation (e.g. delete runs)."""
