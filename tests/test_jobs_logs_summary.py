"""Job history on large stores (cli-server-dotnet#96): lists may leave the logs
out (with ``log_count``), one run keeps them, and ``summarize_executions``
counts runs per job and status."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from qodalis_cli_jobs import CliJobsBuilder, InMemoryJobStorageProvider
from qodalis_cli_jobs.cli_job_scheduler import INTERRUPTED_ERROR, CliJobScheduler
from qodalis_cli_server_abstractions.jobs import (
    CliJobOptions,
    ICliJob,
    ICliJobExecutionContext,
    ICliJobStorageProvider,
    JobExecution,
    JobExecutionQuery,
    JobExecutionSummary,
    JobLogEntry,
    JobState,
)


class IdleJob(ICliJob):
    async def execute_async(self, context: ICliJobExecutionContext, cancellation_event: asyncio.Event) -> None:
        pass


def _without_logs(e: JobExecution) -> JobExecution:
    return dataclasses.replace(e, logs=[], log_count=len(e.logs))


class LogLessListStore(InMemoryJobStorageProvider):
    """A store that lists runs without their logs, as a database store does."""

    async def get_executions(self, job_id, *, limit=20, offset=0, status=None):
        items, total = await super().get_executions(job_id, limit=limit, offset=offset, status=status)
        return [_without_logs(e) for e in items], total

    async def query_executions(self, query):
        items, total = await super().query_executions(query)
        return (items if query.include_logs else [_without_logs(e) for e in items]), total


class ListOnlyStore(ICliJobStorageProvider):
    """A third-party store with only the required methods (no query or summary)."""

    def __init__(self) -> None:
        self.inner = InMemoryJobStorageProvider()

    async def save_execution(self, execution):
        await self.inner.save_execution(execution)

    async def get_executions(self, job_id, *, limit=20, offset=0, status=None):
        return await self.inner.get_executions(job_id, limit=limit, offset=offset, status=status)

    async def get_execution(self, execution_id):
        return await self.inner.get_execution(execution_id)

    async def save_job_state(self, job_id, state: JobState):
        await self.inner.save_job_state(job_id, state)

    async def get_job_state(self, job_id):
        return await self.inner.get_job_state(job_id)

    async def get_all_job_states(self):
        return await self.inner.get_all_job_states()


NOW = datetime.datetime.now(datetime.UTC).replace(microsecond=0)


def _ago(hours: float) -> datetime.datetime:
    return NOW - datetime.timedelta(hours=hours)


def _run(id: str, job_id: str, status: str, started: datetime.datetime, logs: int = 1) -> JobExecution:
    return JobExecution(
        id=id, job_id=job_id, job_name=job_id, status=status, started_at=started,
        logs=[JobLogEntry(timestamp=started + datetime.timedelta(seconds=i + 1), message=f"line {i + 1}") for i in range(logs)],
    )


async def _seed(storage: ICliJobStorageProvider) -> None:
    cases = [("a", "completed", 1), ("a", "completed", 3), ("a", "failed", 2),
             ("b", "failed", 5), ("a", "completed", 30), ("c", "completed", 48)]
    for n, (job, status, hours) in enumerate(cases):
        await storage.save_execution(_run(f"r{n}", job, status, _ago(hours)))


def _assert_summary(summary: list[JobExecutionSummary]) -> None:
    assert [f"{s.job_id}:{s.status}:{s.count}" for s in summary] == ["a:completed:2", "a:failed:1", "b:failed:1"]
    assert summary[0].last_started_at == _ago(1)
    assert summary[2].last_started_at == _ago(5)


class TestSummary:
    async def test_the_in_memory_store_groups_by_job_and_status_since_a_time(self):
        storage = InMemoryJobStorageProvider()
        await _seed(storage)

        _assert_summary(await storage.summarize_executions(_ago(24)))
        assert [s.job_id for s in await storage.summarize_executions(_ago(24), ["b"])] == ["b"]
        assert await storage.summarize_executions(NOW + datetime.timedelta(minutes=1)) == []

    async def test_a_store_without_a_summary_gets_the_default_from_its_listings(self):
        store = ListOnlyStore()
        await _seed(store)

        # The default query merges the per-job listings, so it needs the job ids
        _assert_summary(await store.summarize_executions(_ago(24), ["a", "b", "c"]))
        with pytest.raises(NotImplementedError):
            await store.summarize_executions(_ago(24))


class TestLogsInLists:
    async def test_the_in_memory_store_keeps_them(self):
        storage = InMemoryJobStorageProvider()
        await storage.save_execution(_run("r1", "a", "completed", NOW, logs=2))

        items, _ = await storage.get_executions("a")
        assert len(items[0].logs) == 2
        assert items[0].log_count is None
        assert items[0].log_entry_count == 2

    async def test_the_api_lists_the_stores_log_count_and_reads_the_logs_from_one_run(self):
        storage = LogLessListStore()
        plugin = (
            CliJobsBuilder()
            .set_storage_provider(storage)
            .add_job(IdleJob(), CliJobOptions(id="j", name="j", interval="999s"))
            .build()
        )
        await storage.save_execution(_run("r1", "j", "completed", NOW, logs=2))
        app = FastAPI()
        app.include_router(plugin.router, prefix="/jobs")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            history = (await client.get("/jobs/j/history")).json()
            assert history["items"][0]["logCount"] == 2
            assert "logs" not in history["items"][0]

            across = (await client.get("/jobs/executions", params={"job": "j"})).json()
            assert across["items"][0]["logCount"] == 2

            detail = (await client.get("/jobs/j/history/r1")).json()
            assert [line["message"] for line in detail["logs"]] == ["line 1", "line 2"]

    async def test_the_scheduler_asks_for_the_logs_of_interrupted_runs(self):
        storage = LogLessListStore()
        started = NOW - datetime.timedelta(minutes=10)
        await storage.save_execution(_run("left", "j", "running", started, logs=3))

        scheduler = CliJobScheduler(storage)
        scheduler.register(IdleJob(), CliJobOptions(id="j", name="j", interval="1h"))
        await scheduler.start()
        try:
            left = await storage.get_execution("left")
            assert (left.status, left.error) == ("cancelled", INTERRUPTED_ERROR)
            assert (left.completed_at, left.duration) == (started + datetime.timedelta(seconds=3), 3000)
            assert len(left.logs) == 3
        finally:
            await scheduler.stop()


def test_include_logs_defaults_to_false():
    assert JobExecutionQuery().include_logs is False
