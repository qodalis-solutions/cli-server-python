"""Jobs: max concurrent runs, and no job started twice
(spec 2026-11-06-job-concurrency-design)."""

from __future__ import annotations

import asyncio
import datetime
from typing import Any, Callable

import pytest
from croniter import croniter
from fastapi.testclient import TestClient

from qodalis_cli import CliEvent, CliEventBus, create_cli_server
from qodalis_cli_jobs import (
    CliJobScheduler,
    CliJobsBuilder,
    InMemoryJobStorageProvider,
    InvalidOperationError,
)
from qodalis_cli_jobs.cli_job_scheduler import INTERRUPTED_ERROR
from qodalis_cli_server_abstractions.jobs import (
    CliJobOptions,
    ICliJob,
    JobExecution,
    JobLogEntry,
    JobState,
)

JOBS = "/api/v1/qcli/jobs"


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class GateJob(ICliJob):
    """Runs until ``open()`` or until its run is cancelled; counts its runs.
    With ``fail_first``, the first attempt fails at once."""

    def __init__(self, fail_first: bool = False) -> None:
        self.started = 0
        self.running = 0
        self.peak = 0
        self.cancellations: list[asyncio.Event] = []
        self._gate = asyncio.Event()
        self._fail_first = fail_first

    def open(self) -> None:
        self._gate.set()

    async def execute_async(self, context: Any, cancellation_event: asyncio.Event) -> None:
        self.started += 1
        if self._fail_first and self.started == 1:
            raise RuntimeError("first attempt fails")
        self.running += 1
        self.peak = max(self.peak, self.running)
        self.cancellations.append(cancellation_event)
        try:
            while not self._gate.is_set() and not cancellation_event.is_set():
                await asyncio.sleep(0.002)
        finally:
            self.running -= 1


async def until(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.002)


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.fixture()
def storage() -> InMemoryJobStorageProvider:
    return InMemoryJobStorageProvider()


@pytest.fixture()
async def scheduler(storage: InMemoryJobStorageProvider):
    scheduler = CliJobScheduler(storage)
    yield scheduler
    await scheduler.stop()
    await settle()


def _register(scheduler: CliJobScheduler, job: ICliJob, **options: Any) -> Any:
    options.setdefault("interval", "1h")
    job_id = scheduler.register(job, CliJobOptions(name="job", **options))
    scheduler._running = True  # started, without the start-up timers
    return scheduler.jobs[job_id]


async def _due_runs(scheduler: CliJobScheduler, reg: Any, count: int) -> None:
    """``count`` timer ticks and ``count`` triggers at the same moment."""
    await asyncio.gather(
        *(scheduler._timer_fire(reg, 0) for _ in range(count)),
        *(scheduler.trigger(reg.id) for _ in range(count)),
        return_exceptions=True,
    )


# =========================================================================
# The limit
# =========================================================================


class TestMaxConcurrentRuns:
    @pytest.mark.parametrize("limit", [1, 2])
    async def test_concurrent_due_runs_never_exceed_the_limit(self, scheduler, limit: int):
        job = GateJob()
        reg = _register(scheduler, job, max_concurrent_runs=limit)
        await _due_runs(scheduler, reg, 5)
        await until(lambda: job.running == limit)
        await settle()
        assert job.started == limit and job.peak == limit
        assert len(reg.running_execution_ids) == limit
        job.open()
        await until(lambda: not reg.runs)
        assert job.peak == limit

    async def test_a_trigger_is_refused_when_full_with_skip(self, scheduler):
        job = GateJob()
        reg = _register(scheduler, job, max_concurrent_runs=2)
        assert await scheduler.trigger(reg.id) == "started"
        assert await scheduler.trigger(reg.id) == "started"
        with pytest.raises(InvalidOperationError):
            await scheduler.trigger(reg.id)
        await until(lambda: job.running == 2)
        job.open()

    async def test_lowering_the_limit_cancels_nothing(self, scheduler):
        job = GateJob()
        reg = _register(scheduler, job, max_concurrent_runs=2)
        await scheduler.trigger(reg.id)
        await scheduler.trigger(reg.id)
        await until(lambda: job.running == 2)
        await scheduler.update_options(reg.id, max_concurrent_runs=1)
        await scheduler._timer_fire(reg, 0)  # still 2 running: no new run
        await settle()
        assert job.started == 2 and not any(c.is_set() for c in job.cancellations)
        job.open()
        await until(lambda: not reg.runs)
        assert await scheduler.trigger(reg.id) == "started"

    async def test_raising_the_limit_starts_a_queued_run(self, scheduler):
        job = GateJob()
        reg = _register(scheduler, job, overlap_policy="queue")
        await scheduler.trigger(reg.id)
        assert await scheduler.trigger(reg.id) == "queued"
        await scheduler.update_options(reg.id, max_concurrent_runs=2)
        await until(lambda: job.running == 2)
        assert reg.queued is False
        job.open()

    @pytest.mark.parametrize("value", [0, -1, True, 1.5, "2"])
    async def test_invalid_values_are_refused(self, scheduler, value: Any):
        reg = _register(scheduler, GateJob())
        with pytest.raises(ValueError):
            await scheduler.update_options(reg.id, max_concurrent_runs=value)
        with pytest.raises(ValueError):
            scheduler.register(GateJob(), CliJobOptions(name="bad", max_concurrent_runs=value))


# =========================================================================
# Retries hold the slot
# =========================================================================


class TestRetries:
    async def test_retry_delay_keeps_the_slot_with_skip(self, scheduler):
        job = GateJob(fail_first=True)
        reg = _register(scheduler, job, max_retries=1, retry_delay="1h", retry_strategy="fixed")
        await scheduler.trigger(reg.id)
        await until(lambda: reg.last_run_status == "failed")
        # Waiting for the retry: the run holds its slot, with no execution.
        assert len(reg.runs) == 1 and reg.running_execution_ids == []
        await scheduler._timer_fire(reg, 0)
        with pytest.raises(InvalidOperationError):
            await scheduler.trigger(reg.id)
        await settle()
        assert job.started == 1
        await scheduler.cancel_current(reg.id)  # cancels the retry to come
        await until(lambda: not reg.runs)
        assert job.started == 1

    async def test_retry_delay_keeps_the_slot_with_queue(self, scheduler):
        job = GateJob(fail_first=True)
        reg = _register(scheduler, job, overlap_policy="queue", max_retries=1, retry_delay="0s")
        reg.options.retry_delay = "1h"
        await scheduler.trigger(reg.id)
        await until(lambda: reg.last_run_status == "failed")
        await scheduler._timer_fire(reg, 0)
        await settle()
        assert job.started == 1 and reg.queued is True


# =========================================================================
# Overlap policies
# =========================================================================


class TestOverlapPolicies:
    async def test_queue_coalesces(self, scheduler):
        job = GateJob()
        reg = _register(scheduler, job, overlap_policy="queue")
        await scheduler.trigger(reg.id)
        await until(lambda: job.running == 1)
        # Three due runs while full: one queued run.
        await scheduler._timer_fire(reg, 0)
        assert await scheduler.trigger(reg.id) == "queued"
        await scheduler._timer_fire(reg, 0)
        assert reg.queued is True and job.started == 1
        job.open()
        await until(lambda: not reg.runs and not reg.queued)
        await settle()
        assert job.started == 2 and job.peak == 1

    async def test_cancel_cancels_the_oldest_run(self, scheduler):
        job = GateJob()
        reg = _register(scheduler, job, overlap_policy="cancel", max_concurrent_runs=2)
        await scheduler.trigger(reg.id)
        await until(lambda: job.running == 1)
        await scheduler.trigger(reg.id)
        await until(lambda: job.running == 2)
        oldest, newer = job.cancellations
        await scheduler.trigger(reg.id)
        assert oldest.is_set() and not newer.is_set()
        await until(lambda: job.started == 3)
        assert len(reg.runs) == 2
        job.open()

    async def test_cancel_and_stop_cancel_every_run_and_the_queued_one(self, scheduler):
        job = GateJob()
        reg = _register(scheduler, job, overlap_policy="queue", max_concurrent_runs=2)
        for _ in range(3):
            await scheduler.trigger(reg.id)
        await until(lambda: job.running == 2)
        assert reg.queued is True
        await scheduler.cancel_current(reg.id)
        assert reg.queued is False and all(c.is_set() for c in job.cancellations)
        await until(lambda: not reg.runs)
        assert job.started == 2

        await scheduler.trigger(reg.id)
        await scheduler.trigger(reg.id)
        await scheduler.trigger(reg.id)
        await until(lambda: job.running == 2)
        await scheduler.stop_job(reg.id)
        assert reg.queued is False and reg.status == "stopped"
        await until(lambda: not reg.runs)
        assert job.started == 4


# =========================================================================
# Run on start, cron, interrupted runs
# =========================================================================


class CountingJob(ICliJob):
    def __init__(self) -> None:
        self.calls = 0

    async def execute_async(self, context: Any, cancellation_event: asyncio.Event) -> None:
        self.calls += 1


class TestRunOnStart:
    async def test_interval_job_runs_only_when_due_or_never_ran(self, scheduler, storage):
        recent, due, never = CountingJob(), CountingJob(), CountingJob()
        await storage.save_job_state("recent", JobState(last_run_at=now() - datetime.timedelta(minutes=5)))
        await storage.save_job_state("due", JobState(last_run_at=now() - datetime.timedelta(hours=2)))
        scheduler.register(recent, CliJobOptions(name="recent", interval="1h", run_on_start=True))
        scheduler.register(due, CliJobOptions(name="due", interval="1h", run_on_start=True))
        scheduler.register(never, CliJobOptions(name="never", interval="1h", run_on_start=True))
        await scheduler.start()
        await until(lambda: due.calls == 1 and never.calls == 1)
        await settle()
        assert (recent.calls, due.calls, never.calls) == (0, 1, 1)
        # The recent job keeps the rhythm of its last run.
        expected = scheduler.jobs["recent"].last_run_at + datetime.timedelta(hours=1)
        assert abs((scheduler.jobs["recent"].next_run_at - expected).total_seconds()) < 1

    async def test_cron_job_runs_only_after_a_missed_occurrence(self, scheduler, storage):
        recent, missed, never = CountingJob(), CountingJob(), CountingJob()
        await storage.save_job_state("recent", JobState(last_run_at=now()))
        await storage.save_job_state("missed", JobState(last_run_at=now() - datetime.timedelta(days=2)))
        for name, job in (("recent", recent), ("missed", missed), ("never", never)):
            scheduler.register(job, CliJobOptions(name=name, schedule="0 3 * * *", run_on_start=True))
        await scheduler.start()
        await until(lambda: missed.calls == 1 and never.calls == 1)
        await settle()
        assert (recent.calls, missed.calls, never.calls) == (0, 1, 1)

    async def test_job_without_schedule_runs_on_every_start(self, storage):
        job = CountingJob()
        for _ in range(2):
            scheduler = CliJobScheduler(storage)
            scheduler.register(job, CliJobOptions(name="manual", run_on_start=True))
            await scheduler.start()
            await until(lambda: scheduler.jobs["manual"].last_run_status == "completed")
            await scheduler.stop()
        assert job.calls == 2

    async def test_run_on_start_takes_a_slot(self, scheduler, storage):
        job = GateJob()
        scheduler.register(job, CliJobOptions(name="manual", run_on_start=True))
        await scheduler.start()
        await until(lambda: job.running == 1)
        with pytest.raises(InvalidOperationError):
            await scheduler.trigger("manual")
        job.open()


class TestCron:
    async def test_an_early_tick_does_not_run_the_occurrence_twice(self, scheduler):
        job = CountingJob()
        reg = _register(scheduler, job, interval=None, schedule="* * * * *")
        occurrence = croniter("* * * * *", now()).get_next(datetime.datetime)
        reg.next_run_at = occurrence  # in the future: the tick fires early
        await scheduler._timer_fire(reg, 0)
        await until(lambda: job.calls == 1)
        assert reg.next_run_at == occurrence + datetime.timedelta(minutes=1)

    async def test_a_late_tick_schedules_from_now(self, scheduler):
        reg = _register(scheduler, CountingJob(), interval=None, schedule="* * * * *")
        reg.next_run_at = now() - datetime.timedelta(minutes=10)  # the process was busy
        await scheduler._timer_fire(reg, 0)
        expected = croniter("* * * * *", now()).get_next(datetime.datetime)
        assert reg.next_run_at == expected


class TestInterruptedRuns:
    async def test_running_runs_of_a_previous_process_are_finished_as_cancelled(self, storage):
        started = now() - datetime.timedelta(minutes=10)
        logged = started + datetime.timedelta(seconds=30)
        await storage.save_execution(JobExecution(id="a", job_id="job", status="running", started_at=started))
        await storage.save_execution(JobExecution(
            id="b", job_id="job", status="running", started_at=started,
            logs=[JobLogEntry(timestamp=logged, message="working")],
        ))
        await storage.save_execution(JobExecution(id="c", job_id="job", status="completed", started_at=started))
        await storage.save_execution(JobExecution(id="other", job_id="unknown", status="running"))

        bus = CliEventBus()
        events: list[CliEvent] = []
        bus.subscribe("*", events.append)
        scheduler = CliJobScheduler(storage, event_bus=bus)
        scheduler.register(CountingJob(), CliJobOptions(name="job", interval="1h"))
        await scheduler.start()
        try:
            a = await storage.get_execution("a")
            assert (a.status, a.error, a.completed_at, a.duration) == ("cancelled", INTERRUPTED_ERROR, started, 0)
            b = await storage.get_execution("b")
            assert (b.status, b.completed_at, b.duration) == ("cancelled", logged, 30000)
            assert (await storage.get_execution("c")).status == "completed"
            # Only the scheduler's own jobs are touched.
            assert (await storage.get_execution("other")).status == "running"
            assert events == []
        finally:
            await scheduler.stop()


# =========================================================================
# API
# =========================================================================


class TestApi:
    def test_job_info_update_and_queued(self):
        result = create_cli_server()
        job = GateJob()
        jobs = CliJobsBuilder().add_job(job, CliJobOptions(id="gate", overlap_policy="queue")).build()
        result.mount_plugin(jobs)
        reg = jobs.scheduler.jobs["gate"]
        with TestClient(result.app) as c:
            info = c.get(f"{JOBS}/gate").json()
            assert info["maxConcurrentRuns"] == 1
            assert info["runningExecutionIds"] == [] and info["queued"] is False
            assert info["currentExecutionId"] is None

            for bad in (0, -1, "2", 1.5, True, "x"):
                res = c.put(f"{JOBS}/gate", json={"maxConcurrentRuns": bad})
                assert res.status_code == 400, (bad, res.text)
            assert c.get(f"{JOBS}/gate").json()["maxConcurrentRuns"] == 1

            assert c.post(f"{JOBS}/gate/trigger").json() == {"message": "Job triggered", "queued": False}
            for _ in range(200):
                if reg.running_execution_ids:
                    break
                c.get(f"{JOBS}/gate")
            assert c.post(f"{JOBS}/gate/trigger").json() == {"message": "Job queued", "queued": True}
            info = c.get(f"{JOBS}/gate").json()
            assert info["queued"] is True
            assert info["runningExecutionIds"] == reg.running_execution_ids and len(info["runningExecutionIds"]) == 1
            assert info["currentExecutionId"] == info["runningExecutionIds"][0]
            assert c.get(JOBS).json()[0]["maxConcurrentRuns"] == 1

            events: list[CliEvent] = []
            result.event_bus.subscribe("job:updated", events.append)
            assert c.put(f"{JOBS}/gate", json={"maxConcurrentRuns": 2}).status_code == 200
            assert [e.data for e in events] == [{"jobId": "gate"}]
            for _ in range(200):
                if len(reg.running_execution_ids) == 2:
                    break
                c.get(f"{JOBS}/gate")
            info = c.get(f"{JOBS}/gate").json()
            assert info["maxConcurrentRuns"] == 2 and info["queued"] is False
            assert len(info["runningExecutionIds"]) == 2
            c.portal.call(_open, job)

    def test_mcp_info_and_queued_trigger(self):
        result = create_cli_server(__import__("qodalis_cli").CliServerOptions(mcp=True))
        job = GateJob()
        jobs = CliJobsBuilder().add_job(job, CliJobOptions(id="gate", overlap_policy="queue")).build()
        result.mount_plugin(jobs)

        def call(c: TestClient, name: str) -> dict:
            res = c.post("/api/qcli/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": {"id": "gate"}},
            })
            assert res.status_code == 200, res.text
            return res.json()["result"]

        with TestClient(result.app) as c:
            info = call(c, "jobs_get")["structuredContent"]
            assert info["maxConcurrentRuns"] == 1 and info["queued"] is False
            assert info["runningExecutionIds"] == []
            assert call(c, "jobs_trigger")["content"][0]["text"] == "Job triggered"
            assert call(c, "jobs_trigger")["content"][0]["text"] == "Job queued"
            assert call(c, "jobs_get")["structuredContent"]["queued"] is True
            c.portal.call(_open, job)


async def _open(job: GateJob) -> None:
    job.open()
