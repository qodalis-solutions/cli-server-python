"""Tests for the jobs scheduling system."""

from __future__ import annotations

import asyncio
import datetime

import pytest

from qodalis_cli_server_abstractions.jobs import (
    CliJobOptions,
    ICliJob,
    ICliJobExecutionContext,
    JobExecution,
    JobState,
)

from qodalis_cli_jobs import (
    CliJobExecutionContext,
    CliJobLogger,
    CliJobScheduler,
    InMemoryJobStorageProvider,
    InvalidOperationError,
    parse_interval,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class DummyJob(ICliJob):
    def __init__(self, sleep: float = 0.0, fail: bool = False) -> None:
        self._sleep = sleep
        self._fail = fail
        self.call_count = 0

    async def execute_async(
        self, context: ICliJobExecutionContext, cancellation_event: asyncio.Event
    ) -> None:
        self.call_count += 1
        context.logger.info("started")
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if cancellation_event.is_set():
            context.logger.warning("cancelled")
            return
        if self._fail:
            raise RuntimeError("boom")
        context.logger.info("done")


class SlowCancellableJob(ICliJob):
    async def execute_async(
        self, context: ICliJobExecutionContext, cancellation_event: asyncio.Event
    ) -> None:
        context.logger.info("starting slow job")
        for _ in range(50):
            if cancellation_event.is_set():
                context.logger.info("detected cancellation")
                return
            await asyncio.sleep(0.02)
        context.logger.info("slow job completed")


# ---------------------------------------------------------------------------
# Interval parser
# ---------------------------------------------------------------------------

class TestIntervalParser:
    def test_seconds(self) -> None:
        assert parse_interval("30s") == 30.0

    def test_minutes(self) -> None:
        assert parse_interval("5m") == 300.0

    def test_hours(self) -> None:
        assert parse_interval("1h") == 3600.0

    def test_days(self) -> None:
        assert parse_interval("1d") == 86400.0

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_interval("abc")

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_interval("")


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class TestCliJobLogger:
    def test_captures_entries(self) -> None:
        log = CliJobLogger()
        log.debug("d")
        log.info("i")
        log.warning("w")
        log.error("e")
        assert len(log.entries) == 4
        assert log.entries[0].level == "debug"
        assert log.entries[1].level == "info"
        assert log.entries[2].level == "warning"
        assert log.entries[3].level == "error"
        assert log.entries[0].message == "d"


# ---------------------------------------------------------------------------
# Execution context
# ---------------------------------------------------------------------------

class TestCliJobExecutionContext:
    def test_logger_captures(self) -> None:
        ctx = CliJobExecutionContext()
        ctx.logger.info("hello")
        assert len(ctx.log_entries) == 1
        assert ctx.log_entries[0].message == "hello"


# ---------------------------------------------------------------------------
# In-memory storage
# ---------------------------------------------------------------------------

class TestInMemoryJobStorageProvider:
    @pytest.fixture()
    def storage(self) -> InMemoryJobStorageProvider:
        return InMemoryJobStorageProvider()

    async def test_save_and_get_execution(self, storage: InMemoryJobStorageProvider) -> None:
        ex = JobExecution(id="e1", job_id="j1", job_name="test", status="completed")
        await storage.save_execution(ex)
        result = await storage.get_execution("e1")
        assert result is not None
        assert result.id == "e1"

    async def test_get_executions_pagination(self, storage: InMemoryJobStorageProvider) -> None:
        for i in range(5):
            await storage.save_execution(
                JobExecution(id=f"e{i}", job_id="j1", job_name="t", status="completed")
            )
        items, total = await storage.get_executions("j1", limit=2, offset=1)
        assert total == 5
        assert len(items) == 2

    async def test_get_executions_status_filter(self, storage: InMemoryJobStorageProvider) -> None:
        await storage.save_execution(
            JobExecution(id="e1", job_id="j1", job_name="t", status="completed")
        )
        await storage.save_execution(
            JobExecution(id="e2", job_id="j1", job_name="t", status="failed")
        )
        items, total = await storage.get_executions("j1", status="failed")
        assert total == 1
        assert items[0].id == "e2"

    async def test_save_and_get_job_state(self, storage: InMemoryJobStorageProvider) -> None:
        state = JobState(status="paused")
        await storage.save_job_state("j1", state)
        result = await storage.get_job_state("j1")
        assert result is not None
        assert result.status == "paused"

    async def test_get_all_job_states(self, storage: InMemoryJobStorageProvider) -> None:
        await storage.save_job_state("j1", JobState(status="active"))
        await storage.save_job_state("j2", JobState(status="stopped"))
        states = await storage.get_all_job_states()
        assert len(states) == 2

    async def test_get_nonexistent_execution(self, storage: InMemoryJobStorageProvider) -> None:
        result = await storage.get_execution("nope")
        assert result is None

    async def test_get_nonexistent_state(self, storage: InMemoryJobStorageProvider) -> None:
        result = await storage.get_job_state("nope")
        assert result is None

    async def test_update_execution(self, storage: InMemoryJobStorageProvider) -> None:
        ex = JobExecution(id="e1", job_id="j1", job_name="t", status="running")
        await storage.save_execution(ex)
        ex.status = "completed"
        await storage.save_execution(ex)
        result = await storage.get_execution("e1")
        assert result is not None
        assert result.status == "completed"
        items, total = await storage.get_executions("j1")
        assert total == 1  # not duplicated


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class TestCliJobScheduler:
    @pytest.fixture()
    def storage(self) -> InMemoryJobStorageProvider:
        return InMemoryJobStorageProvider()

    @pytest.fixture()
    def scheduler(self, storage: InMemoryJobStorageProvider) -> CliJobScheduler:
        return CliJobScheduler(storage)

    def test_register_returns_id(self, scheduler: CliJobScheduler) -> None:
        job_id = scheduler.register(DummyJob(), CliJobOptions(name="test", interval="10s"))
        assert job_id in scheduler.registrations

    def test_register_defaults_name_to_class(self, scheduler: CliJobScheduler) -> None:
        job_id = scheduler.register(DummyJob(), CliJobOptions(interval="10s"))
        reg = scheduler.registrations[job_id]
        assert reg.options.name == "DummyJob"

    def test_register_disabled_sets_stopped(self, scheduler: CliJobScheduler) -> None:
        job_id = scheduler.register(
            DummyJob(), CliJobOptions(name="t", interval="10s", enabled=False)
        )
        assert scheduler.registrations[job_id].status == "stopped"

    async def test_trigger_executes_job(
        self, scheduler: CliJobScheduler, storage: InMemoryJobStorageProvider
    ) -> None:
        job = DummyJob()
        job_id = scheduler.register(job, CliJobOptions(name="t", interval="999s"))
        scheduler._running = True  # simulate started
        await scheduler.trigger(job_id)
        await asyncio.sleep(0.1)  # let the task run
        assert job.call_count == 1
        items, total = await storage.get_executions(job_id)
        assert total == 1
        assert items[0].status == "completed"

    async def test_trigger_not_found_raises(self, scheduler: CliJobScheduler) -> None:
        scheduler._running = True
        with pytest.raises(KeyError):
            await scheduler.trigger("nonexistent")

    async def test_trigger_overlap_skip_raises(
        self, scheduler: CliJobScheduler
    ) -> None:
        job = DummyJob(sleep=1.0)
        job_id = scheduler.register(
            job, CliJobOptions(name="t", interval="999s", overlap_policy="skip")
        )
        scheduler._running = True
        await scheduler.trigger(job_id)
        await asyncio.sleep(0.05)
        with pytest.raises(InvalidOperationError):
            await scheduler.trigger(job_id)

    async def test_pause_and_resume(
        self, scheduler: CliJobScheduler, storage: InMemoryJobStorageProvider
    ) -> None:
        job_id = scheduler.register(DummyJob(), CliJobOptions(name="t", interval="10s"))
        scheduler._running = True
        await scheduler.pause(job_id)
        assert scheduler.registrations[job_id].status == "paused"
        state = await storage.get_job_state(job_id)
        assert state is not None
        assert state.status == "paused"

        await scheduler.resume(job_id)
        assert scheduler.registrations[job_id].status == "active"

    async def test_pause_already_paused_raises(self, scheduler: CliJobScheduler) -> None:
        job_id = scheduler.register(DummyJob(), CliJobOptions(name="t", interval="10s"))
        scheduler._running = True
        await scheduler.pause(job_id)
        with pytest.raises(InvalidOperationError):
            await scheduler.pause(job_id)

    async def test_resume_not_paused_raises(self, scheduler: CliJobScheduler) -> None:
        job_id = scheduler.register(DummyJob(), CliJobOptions(name="t", interval="10s"))
        scheduler._running = True
        with pytest.raises(InvalidOperationError):
            await scheduler.resume(job_id)

    async def test_stop_job(self, scheduler: CliJobScheduler) -> None:
        job_id = scheduler.register(DummyJob(), CliJobOptions(name="t", interval="10s"))
        scheduler._running = True
        await scheduler.stop_job(job_id)
        assert scheduler.registrations[job_id].status == "stopped"

    async def test_cancel_current_no_execution_raises(self, scheduler: CliJobScheduler) -> None:
        job_id = scheduler.register(DummyJob(), CliJobOptions(name="t", interval="10s"))
        scheduler._running = True
        with pytest.raises(InvalidOperationError):
            await scheduler.cancel_current(job_id)

    async def test_cancel_current_sets_event(self, scheduler: CliJobScheduler) -> None:
        job = SlowCancellableJob()
        job_id = scheduler.register(job, CliJobOptions(name="t", interval="999s"))
        scheduler._running = True
        await scheduler.trigger(job_id)
        await asyncio.sleep(0.05)
        await scheduler.cancel_current(job_id)
        await asyncio.sleep(0.2)
        items, _ = await scheduler.storage.get_executions(job_id)
        assert len(items) >= 1
        assert items[0].status == "cancelled"

    async def test_failed_job_with_retry(
        self, scheduler: CliJobScheduler, storage: InMemoryJobStorageProvider
    ) -> None:
        job = DummyJob(fail=True)
        job_id = scheduler.register(
            job, CliJobOptions(name="t", interval="999s", max_retries=2, retry_delay="1s", retry_strategy="fixed")
        )
        scheduler._running = True
        await scheduler.trigger(job_id)
        await asyncio.sleep(3.0)
        assert job.call_count == 3  # 1 original + 2 retries
        items, total = await storage.get_executions(job_id)
        assert total == 3
        assert all(e.status == "failed" for e in items)

    async def test_timeout_cancels_job(
        self, scheduler: CliJobScheduler, storage: InMemoryJobStorageProvider
    ) -> None:
        job = DummyJob(sleep=5.0)
        job_id = scheduler.register(
            job, CliJobOptions(name="t", interval="999s", timeout="1s")
        )
        scheduler._running = True
        await scheduler.trigger(job_id)
        await asyncio.sleep(1.5)
        items, _ = await storage.get_executions(job_id)
        assert len(items) == 1
        assert items[0].status == "timed_out"

    async def test_update_options(self, scheduler: CliJobScheduler) -> None:
        job_id = scheduler.register(DummyJob(), CliJobOptions(name="t", interval="10s"))
        scheduler._running = True
        await scheduler.update_options(
            job_id, description="updated", max_retries=3, overlap_policy="queue"
        )
        opts = scheduler.registrations[job_id].options
        assert opts.description == "updated"
        assert opts.max_retries == 3
        assert opts.overlap_policy == "queue"

    async def test_update_options_invalid_cron_raises(self, scheduler: CliJobScheduler) -> None:
        job_id = scheduler.register(DummyJob(), CliJobOptions(name="t", interval="10s"))
        scheduler._running = True
        with pytest.raises(ValueError):
            await scheduler.update_options(job_id, schedule="not-a-cron")

    async def test_update_options_both_schedule_and_interval_raises(
        self, scheduler: CliJobScheduler
    ) -> None:
        job_id = scheduler.register(DummyJob(), CliJobOptions(name="t", interval="10s"))
        scheduler._running = True
        with pytest.raises(ValueError):
            await scheduler.update_options(job_id, schedule="* * * * *", interval="10s")

    async def test_start_and_stop_lifecycle(
        self, scheduler: CliJobScheduler, storage: InMemoryJobStorageProvider
    ) -> None:
        job_id = scheduler.register(DummyJob(), CliJobOptions(name="t", interval="999s"))
        await scheduler.start()
        assert scheduler.registrations[job_id].status == "active"
        assert scheduler.registrations[job_id].next_run_at is not None
        await scheduler.stop()
        state = await storage.get_job_state(job_id)
        assert state is not None

    async def test_start_restores_paused_state(
        self, scheduler: CliJobScheduler, storage: InMemoryJobStorageProvider
    ) -> None:
        job_id = scheduler.register(DummyJob(), CliJobOptions(name="t", interval="10s"))
        await storage.save_job_state(job_id, JobState(status="paused"))
        await scheduler.start()
        assert scheduler.registrations[job_id].status == "paused"
        await scheduler.stop()
