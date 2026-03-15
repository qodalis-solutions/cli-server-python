from __future__ import annotations

import asyncio
import datetime
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from croniter import croniter

from qodalis_cli_server_abstractions.jobs import (
    CliJobOptions,
    ICliJob,
    ICliJobStorageProvider,
    JobExecution,
    JobState,
)

from .cli_job_execution_context import CliJobExecutionContext
from .interval_parser import parse_interval

logger = logging.getLogger(__name__)


@dataclass
class _JobRegistration:
    """Internal runtime state for a registered job."""

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


# Type alias for the broadcast callback.
BroadcastFn = Callable[[str], Awaitable[None]]


class CliJobScheduler:
    """Background job scheduler using asyncio tasks."""

    def __init__(
        self,
        storage: ICliJobStorageProvider,
        broadcast_fn: BroadcastFn | None = None,
    ) -> None:
        self._storage = storage
        self._broadcast_fn = broadcast_fn
        self._registrations: dict[str, _JobRegistration] = {}
        self._running = False

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def registrations(self) -> dict[str, _JobRegistration]:
        return self._registrations

    @property
    def storage(self) -> ICliJobStorageProvider:
        return self._storage

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, job: ICliJob, options: CliJobOptions) -> str:
        """Register a job. Returns the job id."""
        job_id = str(uuid.uuid4())
        if not options.name:
            options.name = type(job).__name__
        if not options.description:
            options.description = options.name
        reg = _JobRegistration(
            id=job_id,
            job=job,
            options=options,
            status="active" if options.enabled else "stopped",
        )
        self._registrations[job_id] = reg
        return job_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load persisted states and start timers for active jobs."""
        self._running = True
        persisted = await self._storage.get_all_job_states()

        for reg in self._registrations.values():
            # Apply persisted state if available
            state = persisted.get(reg.id)
            if state:
                reg.status = state.status
                reg.last_run_at = state.last_run_at

            if reg.status == "active":
                self._schedule_next(reg)

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

    # ------------------------------------------------------------------
    # Control methods
    # ------------------------------------------------------------------

    async def trigger(self, job_id: str) -> None:
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
        reg = self._get_registration(job_id)
        if reg.status == "paused":
            raise InvalidOperationError("Job is already paused")
        reg.status = "paused"
        self._cancel_timer(reg)
        await self._persist_state(reg)
        await self._broadcast({"type": "job:paused", "jobId": job_id})

    async def resume(self, job_id: str) -> None:
        reg = self._get_registration(job_id)
        if reg.status != "paused":
            raise InvalidOperationError("Job is not paused")
        reg.status = "active"
        self._schedule_next(reg)
        await self._persist_state(reg)
        await self._broadcast({"type": "job:resumed", "jobId": job_id})

    async def stop_job(self, job_id: str) -> None:
        reg = self._get_registration(job_id)
        reg.status = "stopped"
        self._cancel_timer(reg)
        if reg.current_cancellation is not None:
            reg.current_cancellation.set()
        await self._persist_state(reg)
        await self._broadcast({"type": "job:stopped", "jobId": job_id})

    async def cancel_current(self, job_id: str) -> None:
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
        timeout: str | None = None,
        overlap_policy: str | None = None,
    ) -> None:
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

    # ------------------------------------------------------------------
    # Internal scheduling
    # ------------------------------------------------------------------

    def _schedule_next(self, reg: _JobRegistration) -> None:
        """Compute next run time and create timer task."""
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
            delay = parse_interval(reg.options.interval)
            reg.next_run_at = now + datetime.timedelta(seconds=delay)
        else:
            # No schedule configured — job can only be triggered manually
            reg.next_run_at = None
            return

        reg.timer_task = asyncio.create_task(self._timer_fire(reg, delay))

    async def _timer_fire(self, reg: _JobRegistration, delay: float) -> None:
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
            self._schedule_next(reg)

    # ------------------------------------------------------------------
    # Job execution
    # ------------------------------------------------------------------

    async def _execute_job(self, reg: _JobRegistration, retry_attempt: int) -> None:
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
        execution.duration = (end_time - execution.started_at).total_seconds() * 1000
        execution.logs = list(ctx.log_entries)
        await self._storage.save_execution(execution)

        reg.last_run_at = execution.started_at
        reg.last_run_status = execution.status
        reg.last_run_duration = execution.duration
        reg.current_execution_id = None
        reg.current_cancellation = None

        if execution.status == "completed":
            await self._broadcast({
                "type": "job:completed",
                "jobId": reg.id,
                "executionId": exec_id,
                "duration": execution.duration,
            })

        # Retry on failure
        if execution.status == "failed" and retry_attempt < reg.options.max_retries:
            logger.info(
                "Retrying job %s (attempt %d/%d)",
                reg.options.name,
                retry_attempt + 1,
                reg.options.max_retries,
            )
            asyncio.create_task(self._execute_job(reg, retry_attempt + 1))
            return

        # Process queue
        if reg.queue:
            queued_attempt = reg.queue.popleft()
            asyncio.create_task(self._execute_job(reg, queued_attempt))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_registration(self, job_id: str) -> _JobRegistration:
        reg = self._registrations.get(job_id)
        if reg is None:
            raise KeyError(f"Job not found: {job_id}")
        return reg

    def _cancel_timer(self, reg: _JobRegistration) -> None:
        if reg.timer_task is not None and not reg.timer_task.done():
            reg.timer_task.cancel()
        reg.timer_task = None
        reg.next_run_at = None

    async def _persist_state(self, reg: _JobRegistration) -> None:
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
