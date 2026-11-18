"""Jobs: history retention (max age, max runs per job, failed limits, per-job
overrides), the ``jobs:prune-history`` system job and ``historyRetention``
in the job info."""

from __future__ import annotations

import asyncio
import datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from qodalis_cli.mcp import McpCallContext
from qodalis_cli_server_abstractions.jobs import (
    CliJobHistoryOptions,
    CliJobOptions,
    ICliJob,
    ICliJobExecutionContext,
    ICliJobStorageProvider,
    JobExecution,
    JobHistoryLimits,
    JobHistoryRetention,
)

from qodalis_cli_jobs import (
    PRUNE_HISTORY_JOB_ID,
    CliJobScheduler,
    CliJobsBuilder,
    InMemoryJobStorageProvider,
)
from qodalis_cli_jobs.mcp_tools import JobsMcpToolProvider

NOW = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.UTC)


class NoopJob(ICliJob):
    async def execute_async(
        self, context: ICliJobExecutionContext, cancellation_event: asyncio.Event
    ) -> None:
        pass


def run(job_id: str, days_ago: float, status: str = "completed", n: int = 0) -> JobExecution:
    return JobExecution(
        id=f"{job_id}-{status}-{days_ago}-{n}",
        job_id=job_id,
        job_name=job_id,
        status=status,
        started_at=NOW - datetime.timedelta(days=days_ago),
    )


async def store_with(*executions: JobExecution) -> InMemoryJobStorageProvider:
    storage = InMemoryJobStorageProvider()
    # Oldest first, as the scheduler saves them
    for e in sorted(executions, key=lambda e: e.started_at):
        await storage.save_execution(e)
    return storage


async def ids(storage: ICliJobStorageProvider, job_id: str) -> set[str]:
    items, _ = await storage.get_executions(job_id, limit=1000)
    return {e.id for e in items}


def scheduler_with(
    storage: ICliJobStorageProvider,
    retention: JobHistoryRetention | None,
    **jobs: CliJobHistoryOptions | None,
) -> CliJobScheduler:
    scheduler = CliJobScheduler(storage, history_retention=retention)
    for job_id, history in jobs.items():
        scheduler.register(NoopJob(), CliJobOptions(id=job_id, history=history))
    return scheduler


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

class TestRetentionLimits:
    async def test_age_limit_deletes_older_runs(self) -> None:
        storage = await store_with(run("a", 1), run("a", 10), run("a", 40), run("a", 50))
        scheduler = scheduler_with(storage, JobHistoryRetention(max_age="30d"), a=None)

        assert await scheduler.prune_history(NOW) == 2
        assert await ids(storage, "a") == {"a-completed-1-0", "a-completed-10-0"}

    async def test_age_limit_accepts_timedelta(self) -> None:
        storage = await store_with(run("a", 1), run("a", 3))
        retention = JobHistoryRetention(max_age=datetime.timedelta(days=2))
        assert await scheduler_with(storage, retention, a=None).prune_history(NOW) == 1

    async def test_count_limit_keeps_newest(self) -> None:
        storage = await store_with(*(run("a", d) for d in range(1, 6)))
        scheduler = scheduler_with(storage, JobHistoryRetention(max_runs_per_job=2), a=None)

        assert await scheduler.prune_history(NOW) == 3
        assert await ids(storage, "a") == {"a-completed-1-0", "a-completed-2-0"}

    async def test_count_limit_is_per_job(self) -> None:
        storage = await store_with(
            *(run("a", d) for d in range(1, 4)), *(run("b", d) for d in range(1, 3)),
        )
        scheduler = scheduler_with(storage, JobHistoryRetention(max_runs_per_job=2), a=None, b=None)

        assert await scheduler.prune_history(NOW) == 1
        assert len(await ids(storage, "a")) == 2
        assert len(await ids(storage, "b")) == 2

    async def test_a_run_breaking_either_limit_goes(self) -> None:
        storage = await store_with(run("a", 1), run("a", 2), run("a", 3), run("a", 40))
        retention = JobHistoryRetention(max_age="30d", max_runs_per_job=2)

        assert await scheduler_with(storage, retention, a=None).prune_history(NOW) == 2
        assert await ids(storage, "a") == {"a-completed-1-0", "a-completed-2-0"}

    async def test_failed_max_age_keeps_failed_runs_longer(self) -> None:
        storage = await store_with(
            run("a", 1), run("a", 40), run("a", 40, "failed"), run("a", 100, "failed"),
            run("a", 40, "timed_out"),
        )
        retention = JobHistoryRetention(max_age="30d", failed_max_age="90d")

        assert await scheduler_with(storage, retention, a=None).prune_history(NOW) == 2
        assert await ids(storage, "a") == {
            "a-completed-1-0", "a-failed-40-0", "a-timed_out-40-0",
        }

    async def test_failed_max_runs_counts_failed_runs_apart(self) -> None:
        storage = await store_with(
            run("a", 1), run("a", 2), run("a", 3),
            run("a", 4, "failed"), run("a", 5, "failed"), run("a", 6, "failed"),
        )
        retention = JobHistoryRetention(max_runs_per_job=1, failed_max_runs_per_job=2)

        assert await scheduler_with(storage, retention, a=None).prune_history(NOW) == 3
        assert await ids(storage, "a") == {
            "a-completed-1-0", "a-failed-4-0", "a-failed-5-0",
        }

    async def test_without_failed_limits_failed_runs_follow_normal_ones(self) -> None:
        storage = await store_with(run("a", 1), run("a", 40, "failed"))
        retention = JobHistoryRetention(max_age="30d")
        assert await scheduler_with(storage, retention, a=None).prune_history(NOW) == 1

    async def test_running_and_latest_runs_are_never_deleted(self) -> None:
        storage = await store_with(
            run("a", 100), run("a", 90, "running"), run("a", 80),
            run("b", 365),
        )
        retention = JobHistoryRetention(max_age="1d", max_runs_per_job=1)

        assert await scheduler_with(storage, retention, a=None, b=None).prune_history(NOW) == 1
        # a: the latest (80) and the running (90) stay; b: its only run is its latest
        assert await ids(storage, "a") == {"a-completed-80-0", "a-running-90-0"}
        assert await ids(storage, "b") == {"b-completed-365-0"}

    async def test_unconfigured_retention_deletes_nothing(self) -> None:
        storage = await store_with(run("a", 1), run("a", 400))
        assert await scheduler_with(storage, None, a=None).prune_history(NOW) == 0
        assert await scheduler_with(storage, JobHistoryRetention(), a=None).prune_history(NOW) == 0
        assert len(await ids(storage, "a")) == 2


# ---------------------------------------------------------------------------
# Per-job overrides
# ---------------------------------------------------------------------------

class TestPerJobOverride:
    async def test_a_set_field_replaces_the_global_one(self) -> None:
        storage = await store_with(
            *(run("noisy", d) for d in range(1, 6)), *(run("report", d * 10) for d in range(1, 6)),
        )
        retention = JobHistoryRetention(max_age="30d", max_runs_per_job=10)
        scheduler = scheduler_with(
            storage, retention,
            noisy=CliJobHistoryOptions(max_runs_per_job=2),
            report=CliJobHistoryOptions(max_age="45d"),
        )

        assert await scheduler.prune_history(NOW) == 3 + 1
        assert len(await ids(storage, "noisy")) == 2
        # report: 10, 20, 30, 40 days stay (45d), 50 goes
        assert len(await ids(storage, "report")) == 4

    async def test_keep_all(self) -> None:
        storage = await store_with(run("monthly", 400), run("monthly", 800), run("a", 400), run("a", 800))
        scheduler = scheduler_with(
            storage, JobHistoryRetention(max_age="30d"),
            monthly=CliJobHistoryOptions(keep_all=True), a=None,
        )

        assert await scheduler.prune_history(NOW) == 1
        assert len(await ids(storage, "monthly")) == 2

    async def test_per_job_limits_without_global_retention(self) -> None:
        storage = await store_with(*(run("a", d) for d in range(1, 4)), *(run("b", d) for d in range(1, 4)))
        scheduler = scheduler_with(storage, None, a=CliJobHistoryOptions(max_runs_per_job=1), b=None)

        assert await scheduler.prune_history(NOW) == 2
        assert len(await ids(storage, "b")) == 3

    def test_effective_limits(self) -> None:
        retention = JobHistoryRetention(max_age="30d", max_runs_per_job=500, failed_max_age="90d")
        limits = JobHistoryLimits.resolve(retention, CliJobHistoryOptions(max_runs_per_job=50))
        assert limits == JobHistoryLimits(
            max_age=datetime.timedelta(days=30),
            max_runs_per_job=50,
            failed_max_age=datetime.timedelta(days=90),
            overridden=True,
        )
        assert JobHistoryLimits.resolve(retention, CliJobHistoryOptions()).overridden is False
        assert JobHistoryLimits.resolve(None, None) is None

    def test_invalid_values_are_refused(self) -> None:
        with pytest.raises(ValueError):
            CliJobScheduler(InMemoryJobStorageProvider(), history_retention=JobHistoryRetention(max_age="30 days"))
        with pytest.raises(ValueError):
            CliJobsBuilder().set_history_retention(JobHistoryRetention(max_runs_per_job=0))
        with pytest.raises(ValueError):
            scheduler_with(InMemoryJobStorageProvider(), None, a=CliJobHistoryOptions(failed_max_runs_per_job=-1))


# ---------------------------------------------------------------------------
# Stores without prune_history
# ---------------------------------------------------------------------------

class AgeOnlyStorage(InMemoryJobStorageProvider):
    """A store that only implements prune(before)."""

    prune_history = ICliJobStorageProvider.prune_history

    def __init__(self) -> None:
        super().__init__()
        self.pruned_before: datetime.datetime | None = None

    async def prune(self, before: datetime.datetime) -> int:
        self.pruned_before = before
        return await super().prune(before)


class TestAgeOnlyFallback:
    async def test_falls_back_to_prune_before_with_the_longer_max_age(self) -> None:
        storage = AgeOnlyStorage()
        for e in (run("a", 1), run("a", 40), run("a", 100)):
            await storage.save_execution(e)
        retention = JobHistoryRetention(max_age="30d", failed_max_age="60d", max_runs_per_job=1)

        assert await scheduler_with(storage, retention, a=None).prune_history(NOW) == 1
        assert storage.pruned_before == NOW - datetime.timedelta(days=60)

    async def test_without_a_global_max_age_nothing_is_pruned(self) -> None:
        storage = AgeOnlyStorage()
        await storage.save_execution(run("a", 100))
        retention = JobHistoryRetention(max_runs_per_job=1)
        assert await scheduler_with(storage, retention, a=None).prune_history(NOW) == 0
        assert storage.pruned_before is None


# ---------------------------------------------------------------------------
# The jobs:prune-history system job
# ---------------------------------------------------------------------------

class TestPruneHistoryJob:
    def test_not_registered_without_retention(self) -> None:
        assert PRUNE_HISTORY_JOB_ID not in CliJobsBuilder().build().scheduler.jobs
        plugin = CliJobsBuilder().set_history_retention(JobHistoryRetention()).build()
        assert PRUNE_HISTORY_JOB_ID not in plugin.scheduler.jobs

    def test_registered_as_a_system_job(self) -> None:
        plugin = (
            CliJobsBuilder()
            .set_history_retention(JobHistoryRetention(max_age="30d", prune_interval="6h"))
            .build()
        )
        reg = plugin.scheduler.jobs[PRUNE_HISTORY_JOB_ID]
        assert reg.id == PRUNE_HISTORY_JOB_ID
        assert reg.options.name == PRUNE_HISTORY_JOB_ID
        assert reg.options.group == "system"
        assert "system" in reg.options.tags
        assert reg.options.interval == "6h"

    def test_default_interval_and_timedelta_interval(self) -> None:
        default = CliJobsBuilder().set_history_retention(JobHistoryRetention(max_runs_per_job=5)).build()
        assert default.scheduler.jobs[PRUNE_HISTORY_JOB_ID].options.interval == "1d"
        custom = CliJobsBuilder().set_history_retention(
            JobHistoryRetention(max_runs_per_job=5, prune_interval=datetime.timedelta(minutes=90))
        ).build()
        assert custom.scheduler.jobs[PRUNE_HISTORY_JOB_ID].options.interval == "90m"

    async def test_a_run_sets_deleted_and_logs_it(self) -> None:
        storage = await store_with(*(run("a", d) for d in range(1, 5)))
        plugin = (
            CliJobsBuilder()
            .set_storage_provider(storage)
            .set_history_retention(JobHistoryRetention(max_runs_per_job=1))
            .add_job(NoopJob(), CliJobOptions(id="a", interval="999s"))
            .build()
        )
        scheduler = plugin.scheduler
        await scheduler.start()
        try:
            assert await scheduler.trigger(PRUNE_HISTORY_JOB_ID) == "started"
            for _ in range(50):
                items, _ = await storage.get_executions(PRUNE_HISTORY_JOB_ID)
                if items and items[0].status != "running":
                    break
                await asyncio.sleep(0.02)
            assert items[0].status == "completed"
            assert items[0].properties == {"deleted": 3}
            assert any("3" in entry.message for entry in items[0].logs)
            assert len(await ids(storage, "a")) == 1

            await scheduler.pause(PRUNE_HISTORY_JOB_ID)
            assert scheduler.jobs[PRUNE_HISTORY_JOB_ID].status == "paused"
        finally:
            await scheduler.stop()


# ---------------------------------------------------------------------------
# historyRetention in the job info (REST and MCP)
# ---------------------------------------------------------------------------

@pytest.fixture()
async def retention_client():
    plugin = (
        CliJobsBuilder()
        .set_history_retention(JobHistoryRetention(max_age="30d", max_runs_per_job=500, failed_max_age="90d"))
        .add_job(NoopJob(), CliJobOptions(id="plain"))
        .add_job(NoopJob(), CliJobOptions(id="noisy", history=CliJobHistoryOptions(max_runs_per_job=50)))
        .add_job(NoopJob(), CliJobOptions(id="monthly", history=CliJobHistoryOptions(keep_all=True)))
        .build()
    )
    app = FastAPI()
    app.include_router(plugin.router, prefix=plugin.prefix)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, plugin


class TestHistoryRetentionJson:
    async def test_job_list_and_detail(self, retention_client) -> None:
        client, _ = retention_client
        jobs = {j["id"]: j for j in (await client.get("/api/v1/qcli/jobs")).json()}

        assert jobs["plain"]["historyRetention"] == {
            "maxAge": "30d", "maxRunsPerJob": 500, "failedMaxAge": "90d",
            "failedMaxRunsPerJob": None, "overridden": False, "keepAll": False,
        }
        assert jobs["noisy"]["historyRetention"]["maxRunsPerJob"] == 50
        assert jobs["noisy"]["historyRetention"]["maxAge"] == "30d"
        assert jobs["noisy"]["historyRetention"]["overridden"] is True
        assert jobs["monthly"]["historyRetention"] == {
            "maxAge": None, "maxRunsPerJob": None, "failedMaxAge": None,
            "failedMaxRunsPerJob": None, "overridden": True, "keepAll": True,
        }
        assert jobs[PRUNE_HISTORY_JOB_ID]["historyRetention"]["overridden"] is False

        detail = (await client.get("/api/v1/qcli/jobs/noisy")).json()
        assert detail["historyRetention"] == jobs["noisy"]["historyRetention"]

    async def test_null_without_retention(self) -> None:
        plugin = CliJobsBuilder().add_job(NoopJob(), CliJobOptions(id="x")).build()
        app = FastAPI()
        app.include_router(plugin.router, prefix=plugin.prefix)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/v1/qcli/jobs/x")).json()["historyRetention"] is None

    async def test_mcp_jobs_list_and_get(self, retention_client) -> None:
        _, plugin = retention_client
        tools = {t.name: t for t in JobsMcpToolProvider(plugin.scheduler).get_tools(McpCallContext())}

        listed = await tools["jobs_list"].call({}, McpCallContext())
        by_id = {j["id"]: j for j in listed.structured["jobs"]}
        assert by_id["monthly"]["historyRetention"]["keepAll"] is True

        got = await tools["jobs_get"].call({"id": "noisy"}, McpCallContext())
        assert got.structured["historyRetention"]["maxRunsPerJob"] == 50
