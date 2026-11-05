"""Jobs: stable ids, interval rhythm, run on start, run properties, discard,
pruning and the cross-job executions listing."""

from __future__ import annotations

import asyncio
import datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from qodalis_cli.mcp import McpCallContext
from qodalis_cli_jobs import (
    CliJobsBuilder,
    CliJobScheduler,
    ICliJobScheduler,
    InMemoryJobStorageProvider,
    InvalidOperationError,
)
from qodalis_cli_jobs.mcp_tools import JobsMcpToolProvider
from qodalis_cli_server_abstractions.jobs import (
    CliJobOptions,
    ICliJob,
    ICliJobExecutionContext,
    ICliJobStorageProvider,
    JobExecution,
    JobExecutionQuery,
    JobState,
)

UTC = datetime.UTC


def now() -> datetime.datetime:
    return datetime.datetime.now(UTC)


class CountingJob(ICliJob):
    """Counts its runs and saves the count in the run's properties."""

    def __init__(self, sleep: float = 0.0) -> None:
        self.calls = 0
        self._sleep = sleep

    async def execute_async(
        self, context: ICliJobExecutionContext, cancellation_event: asyncio.Event
    ) -> None:
        self.calls += 1
        context.properties["calls"] = self.calls
        context.logger.info("ran")
        if self._sleep:
            await asyncio.sleep(self._sleep)


class DiscardingJob(ICliJob):
    def __init__(self, fail: bool) -> None:
        self._fail = fail

    async def execute_async(
        self, context: ICliJobExecutionContext, cancellation_event: asyncio.Event
    ) -> None:
        context.discard()
        await asyncio.sleep(0)
        if self._fail:
            raise RuntimeError("boom")


class PerJobOnlyStorage(ICliJobStorageProvider):
    """A store with only the required methods (default query, prune, delete)."""

    def __init__(self) -> None:
        self.inner = InMemoryJobStorageProvider()

    async def save_execution(self, execution: JobExecution) -> None:
        await self.inner.save_execution(execution)

    async def get_executions(self, job_id, *, limit=20, offset=0, status=None):
        return await self.inner.get_executions(job_id, limit=limit, offset=offset, status=status)

    async def get_execution(self, execution_id):
        return await self.inner.get_execution(execution_id)

    async def save_job_state(self, job_id, state):
        await self.inner.save_job_state(job_id, state)

    async def get_job_state(self, job_id):
        return await self.inner.get_job_state(job_id)

    async def get_all_job_states(self):
        return await self.inner.get_all_job_states()


@pytest.fixture()
def storage() -> InMemoryJobStorageProvider:
    return InMemoryJobStorageProvider()


@pytest.fixture()
async def scheduler(storage: InMemoryJobStorageProvider):
    s = CliJobScheduler(storage)
    yield s
    await s.stop()


# ---------------------------------------------------------------------------
# Stable ids
# ---------------------------------------------------------------------------

class TestJobIds:
    def test_ids_are_the_set_id_or_the_name_numbered_in_order(self, scheduler: CliJobScheduler) -> None:
        scheduler.register(CountingJob(), CliJobOptions(id="fetch-rates:bnr", name="BNR"))
        scheduler.register(CountingJob(), CliJobOptions(name="digest"))
        scheduler.register(CountingJob(), CliJobOptions(name="digest"))
        scheduler.register(CountingJob(), CliJobOptions())

        assert list(scheduler.jobs) == ["fetch-rates:bnr", "digest", "digest-2", "CountingJob"]
        with pytest.raises(InvalidOperationError):
            scheduler.register(CountingJob(), CliJobOptions(id="digest"))
        with pytest.raises(ValueError):
            scheduler.register(CountingJob(), CliJobOptions(id=" "))

    async def test_saved_state_is_found_again_after_a_restart(
        self, scheduler: CliJobScheduler, storage: InMemoryJobStorageProvider,
    ) -> None:
        await storage.save_job_state("nightly", JobState(status="paused"))
        scheduler.register(CountingJob(), CliJobOptions(name="nightly", interval="1h"))

        await scheduler.start()

        assert scheduler.jobs["nightly"].status == "paused"

    def test_ids_are_url_safe(self, scheduler: CliJobScheduler) -> None:
        scheduler.register(CountingJob(), CliJobOptions(name="reports/daily"))

        assert list(scheduler.jobs) == ["reports-daily"]
        with pytest.raises(ValueError):
            scheduler.register(CountingJob(), CliJobOptions(id="a/b"))

    async def test_a_job_the_code_disables_stays_stopped_whatever_was_saved(
        self, scheduler: CliJobScheduler, storage: InMemoryJobStorageProvider,
    ) -> None:
        last_run = now() - datetime.timedelta(hours=2)
        await storage.save_job_state("nightly", JobState(status="active", last_run_at=last_run))
        await storage.save_job_state("weekly", JobState(status="stopped"))
        nightly = CountingJob()
        scheduler.register(nightly, CliJobOptions(name="nightly", interval="1h", enabled=False))
        scheduler.register(CountingJob(), CliJobOptions(name="weekly", interval="1h"))

        await scheduler.start()
        await asyncio.sleep(0.1)

        assert scheduler.jobs["nightly"].status == "stopped"
        assert scheduler.jobs["nightly"].last_run_at == last_run
        assert nightly.calls == 0
        # A stop made at run time is kept
        assert scheduler.jobs["weekly"].status == "stopped"

    def test_scheduler_is_an_ICliJobScheduler_with_read_only_jobs(self, scheduler: CliJobScheduler) -> None:
        scheduler.register(CountingJob(), CliJobOptions(name="a"))
        assert isinstance(scheduler, ICliJobScheduler)
        with pytest.raises(TypeError):
            scheduler.jobs["b"] = scheduler.jobs["a"]  # type: ignore[index]


# ---------------------------------------------------------------------------
# Interval rhythm and run on start
# ---------------------------------------------------------------------------

class TestScheduling:
    async def test_interval_keeps_its_rhythm_and_runs_an_overdue_job_right_away(
        self, scheduler: CliJobScheduler, storage: InMemoryJobStorageProvider,
    ) -> None:
        last_run = now() - datetime.timedelta(minutes=20)
        await storage.save_job_state("hourly", JobState(status="active", last_run_at=last_run))
        await storage.save_job_state(
            "overdue", JobState(status="active", last_run_at=now() - datetime.timedelta(hours=3)),
        )
        overdue = CountingJob()
        scheduler.register(CountingJob(), CliJobOptions(name="hourly", interval="1h"))
        scheduler.register(overdue, CliJobOptions(name="overdue", interval="1h"))

        await scheduler.start()
        await asyncio.sleep(0.2)

        next_run = scheduler.jobs["hourly"].next_run_at
        assert next_run is not None
        assert abs((next_run - (last_run + datetime.timedelta(hours=1))).total_seconds()) < 1
        assert overdue.calls == 1
        # After the run, the next one is an interval later
        assert scheduler.jobs["overdue"].next_run_at > now() + datetime.timedelta(minutes=59)

    async def test_resume_keeps_the_rhythm(self, scheduler: CliJobScheduler) -> None:
        job_id = scheduler.register(CountingJob(), CliJobOptions(name="hourly", interval="1h"))
        await scheduler.start()
        last_run = now() - datetime.timedelta(minutes=30)
        scheduler.jobs[job_id].last_run_at = last_run
        await scheduler.pause(job_id)
        await scheduler.resume(job_id)

        expected = last_run + datetime.timedelta(hours=1)
        assert abs((scheduler.jobs[job_id].next_run_at - expected).total_seconds()) < 1

    async def test_run_on_start_runs_on_demand_jobs_once_but_not_paused_ones(
        self, scheduler: CliJobScheduler, storage: InMemoryJobStorageProvider,
    ) -> None:
        on_demand, hourly, paused, cron = CountingJob(), CountingJob(sleep=0.1), CountingJob(), CountingJob()
        await storage.save_job_state("paused", JobState(status="paused"))
        await storage.save_job_state(
            "hourly", JobState(status="active", last_run_at=now() - datetime.timedelta(hours=2)),
        )
        scheduler.register(on_demand, CliJobOptions(name="backfill", run_on_start=True))
        scheduler.register(hourly, CliJobOptions(name="hourly", interval="1h", run_on_start=True))
        scheduler.register(paused, CliJobOptions(name="paused", run_on_start=True))
        scheduler.register(cron, CliJobOptions(name="cron", schedule="0 3 * * *", run_on_start=True))

        await scheduler.start()
        await asyncio.sleep(0.4)

        assert on_demand.calls == 1
        # Overdue and run on start: still one run
        assert hourly.calls == 1
        assert paused.calls == 0
        assert cron.calls == 1


# ---------------------------------------------------------------------------
# Inside a run: properties and discard
# ---------------------------------------------------------------------------

class TestRunContext:
    async def test_properties_are_saved_with_the_execution(
        self, scheduler: CliJobScheduler, storage: InMemoryJobStorageProvider,
    ) -> None:
        job_id = scheduler.register(CountingJob(), CliJobOptions(name="t"))
        await scheduler.start()
        await scheduler.trigger(job_id)
        await asyncio.sleep(0.1)

        items, total = await storage.get_executions(job_id)
        assert total == 1
        assert items[0].status == "completed"
        assert items[0].properties == {"calls": 1}

    async def test_a_run_whose_properties_cannot_be_saved_is_saved_without_them(self) -> None:
        class RefusingStore(InMemoryJobStorageProvider):
            async def save_execution(self, execution: JobExecution) -> None:
                if execution.properties:
                    raise TypeError("not serializable")
                await super().save_execution(execution)

        storage = RefusingStore()
        scheduler = CliJobScheduler(storage)
        job_id = scheduler.register(CountingJob(), CliJobOptions(name="props"))
        await scheduler.start()
        try:
            await scheduler.trigger(job_id)
            await asyncio.sleep(0.1)

            items, _ = await storage.get_executions(job_id)
            assert items[0].status == "completed"
            assert items[0].properties == {}
            assert any(e.level == "error" and "properties" in e.message for e in items[0].logs)
            assert scheduler.jobs[job_id].current_execution_id is None
        finally:
            await scheduler.stop()

    async def test_discard_leaves_a_completed_run_out_but_keeps_failures(
        self, scheduler: CliJobScheduler, storage: InMemoryJobStorageProvider,
    ) -> None:
        scheduler.register(DiscardingJob(fail=False), CliJobOptions(name="quiet"))
        scheduler.register(DiscardingJob(fail=True), CliJobOptions(name="broken", max_retries=0))
        await scheduler.start()

        await scheduler.trigger("quiet")
        await scheduler.trigger("broken")
        await asyncio.sleep(0.2)

        assert (await storage.get_executions("quiet"))[1] == 0
        assert scheduler.jobs["quiet"].last_run_at is not None
        assert (await storage.get_executions("broken"))[1] == 1

    async def test_discard_keeps_the_run_when_the_store_cannot_delete(self) -> None:
        storage = PerJobOnlyStorage()
        scheduler = CliJobScheduler(storage)
        scheduler.register(DiscardingJob(fail=False), CliJobOptions(name="quiet"))
        await scheduler.start()
        await scheduler.trigger("quiet")
        await asyncio.sleep(0.1)
        await scheduler.stop()

        items, total = await storage.get_executions("quiet")
        assert total == 1 and items[0].status == "completed"


# ---------------------------------------------------------------------------
# Storage: cross-job query, prune, delete
# ---------------------------------------------------------------------------

def _execution(job_id: str, minutes_ago: float, status: str = "completed") -> JobExecution:
    return JobExecution(
        id=f"{job_id}-{minutes_ago}", job_id=job_id, job_name=job_id, status=status,
        started_at=now() - datetime.timedelta(minutes=minutes_ago),
    )


class TestStorage:
    async def test_in_memory_query_across_jobs(self, storage: InMemoryJobStorageProvider) -> None:
        for e in (_execution("a", 3), _execution("b", 2, "failed"), _execution("a", 1)):
            await storage.save_execution(e)

        items, total = await storage.query_executions(JobExecutionQuery())
        assert total == 3 and [e.id for e in items] == ["a-1", "b-2", "a-3"]
        items, total = await storage.query_executions(JobExecutionQuery(job_ids=["b"]))
        assert total == 1
        items, total = await storage.query_executions(JobExecutionQuery(status="FAILED"))
        assert [e.id for e in items] == ["b-2"]
        items, total = await storage.query_executions(
            JobExecutionQuery(since=now() - datetime.timedelta(minutes=2.5)),
        )
        assert total == 2
        items, total = await storage.query_executions(JobExecutionQuery(limit=1, offset=1))
        assert total == 3 and [e.id for e in items] == ["b-2"]

    async def test_default_query_merges_per_job_and_needs_job_ids(self) -> None:
        storage = PerJobOnlyStorage()
        for e in (_execution("a", 3), _execution("b", 2), _execution("a", 1)):
            await storage.save_execution(e)

        items, total = await storage.query_executions(JobExecutionQuery(job_ids=["a", "b"], limit=2))
        assert total == 3 and [e.id for e in items] == ["a-1", "b-2"]
        items, total = await storage.query_executions(
            JobExecutionQuery(job_ids=["a", "b"], since=now() + datetime.timedelta(days=1)),
        )
        assert (items, total) == ([], 0)
        with pytest.raises(NotImplementedError):
            await storage.query_executions(JobExecutionQuery())

    async def test_scheduler_query_merges_the_registered_jobs_for_such_a_store(self) -> None:
        storage = PerJobOnlyStorage()
        scheduler = CliJobScheduler(storage)
        scheduler.register(CountingJob(), CliJobOptions(name="a"))
        scheduler.register(CountingJob(), CliJobOptions(name="b"))
        for e in (_execution("a", 3), _execution("b", 2), _execution("gone", 1)):
            await storage.save_execution(e)

        items, total = await scheduler.query_executions(JobExecutionQuery())

        assert total == 2 and [e.id for e in items] == ["b-2", "a-3"]

    async def test_prune_and_delete(self, storage: InMemoryJobStorageProvider) -> None:
        await storage.save_execution(_execution("a", 60 * 24 * 40))
        await storage.save_execution(_execution("a", 1))

        assert await storage.prune(now() - datetime.timedelta(days=30)) == 1
        assert (await storage.get_executions("a"))[1] == 1
        assert await storage.delete_execution("a-1") is True
        assert await storage.delete_execution("a-1") is False
        assert (await storage.get_executions("a"))[1] == 0

    async def test_store_defaults_keep_everything(self) -> None:
        storage = PerJobOnlyStorage()
        await storage.save_execution(_execution("a", 60))
        assert await storage.prune(now()) == 0
        assert await storage.delete_execution("a-60") is False


# ---------------------------------------------------------------------------
# REST: GET /executions, properties in history and detail; MCP history
# ---------------------------------------------------------------------------

@pytest.fixture()
async def jobs_client():
    plugin = (
        CliJobsBuilder()
        .add_job(CountingJob(), CliJobOptions(name="quick", group="Reports"))
        .add_job(CountingJob(), CliJobOptions(name="slow", group="other"))
        .build()
    )
    app = FastAPI()
    app.include_router(plugin.router, prefix=plugin.prefix)
    await plugin.scheduler.start()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, plugin
    await plugin.scheduler.stop()


class TestExecutionsApi:
    async def test_executions_across_jobs_filter_by_job_group_status_and_since(self, jobs_client) -> None:
        client, _ = jobs_client
        base = "/api/v1/qcli/jobs"
        for _ in range(2):
            assert (await client.post(f"{base}/quick/trigger")).status_code == 200
            await asyncio.sleep(0.1)

        async def get(query: str = "") -> dict:
            res = await client.get(f"{base}/executions{query}")
            assert res.status_code == 200
            return res.json()

        body = await get()
        assert body["total"] == 2 and body["limit"] == 20 and body["offset"] == 0
        assert body["items"][0]["properties"] == {"calls": 2}
        assert body["items"][0]["jobId"] == "quick" and body["items"][0]["jobName"] == "quick"
        assert body["items"][0]["logCount"] == 1

        assert (await get("?job=quick,slow"))["total"] == 2
        assert (await get("?job=slow&job=quick"))["total"] == 2
        assert (await get("?job=slow"))["total"] == 0
        assert (await get("?status=failed"))["total"] == 0
        assert (await get("?status=COMPLETED"))["total"] == 2
        assert (await get("?limit=1&offset=1"))["items"][0]["properties"] == {"calls": 1}
        assert (await get("?group=reports"))["total"] == 2
        assert (await get("?group=reports&job=slow"))["total"] == 0
        # No job in the group: nothing, not every job
        assert (await get("?group=none"))["total"] == 0
        since = (now() + datetime.timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        assert (await get(f"?since={since}"))["total"] == 0
        clamped = await get("?limit=1000&offset=-5")
        assert clamped["limit"] == 100 and clamped["offset"] == 0
        assert (await get("?limit=0"))["limit"] == 1

    async def test_history_and_detail_carry_properties(self, jobs_client) -> None:
        client, _ = jobs_client
        base = "/api/v1/qcli/jobs/quick"
        await client.post(f"{base}/trigger")
        await asyncio.sleep(0.1)

        item = (await client.get(f"{base}/history")).json()["items"][0]
        assert item["properties"] == {"calls": 1}
        detail = (await client.get(f"{base}/history/{item['id']}")).json()
        assert detail["properties"] == {"calls": 1}

    async def test_mcp_jobs_history_carries_properties(self, jobs_client) -> None:
        client, plugin = jobs_client
        await client.post("/api/v1/qcli/jobs/quick/trigger")
        await asyncio.sleep(0.1)

        tools = {t.name: t for t in JobsMcpToolProvider(plugin.scheduler).get_tools(McpCallContext())}
        result = await tools["jobs_history"].call({"id": "quick"}, McpCallContext())

        assert result.structured["items"][0]["properties"] == {"calls": 1}
