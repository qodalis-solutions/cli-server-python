"""Tests for the jobs REST controller."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from qodalis_cli_server_abstractions.jobs import CliJobOptions, ICliJob, ICliJobExecutionContext

from qodalis_cli_jobs import CliJobsBuilder, CliJobsPlugin


# ---------------------------------------------------------------------------
# Test job
# ---------------------------------------------------------------------------

class QuickJob(ICliJob):
    async def execute_async(
        self, context: ICliJobExecutionContext, cancellation_event: asyncio.Event
    ) -> None:
        context.logger.info("quick job ran")


class SlowJob(ICliJob):
    async def execute_async(
        self, context: ICliJobExecutionContext, cancellation_event: asyncio.Event
    ) -> None:
        for _ in range(100):
            if cancellation_event.is_set():
                return
            await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def jobs_plugin() -> CliJobsPlugin:
    return (
        CliJobsBuilder()
        .add_job(QuickJob(), CliJobOptions(name="quick", interval="999s"))
        .add_job(SlowJob(), CliJobOptions(name="slow", interval="999s"))
        .build()
    )


@pytest.fixture()
def app(jobs_plugin: CliJobsPlugin) -> FastAPI:
    app = FastAPI()
    app.include_router(jobs_plugin.router, prefix="/api/v1/qcli/jobs")
    return app


@pytest.fixture()
def job_ids(jobs_plugin: CliJobsPlugin) -> dict[str, str]:
    """Return dict mapping name -> id."""
    return {
        reg.options.name: reg.id
        for reg in jobs_plugin.scheduler.registrations.values()
    }


@pytest.fixture()
async def client(app: FastAPI):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestListJobs:
    async def test_returns_all_jobs(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/qcli/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        names = {j["name"] for j in data}
        assert names == {"quick", "slow"}

    async def test_job_dto_shape(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/qcli/jobs")
        job = resp.json()[0]
        assert "id" in job
        assert "name" in job
        assert "status" in job
        assert "maxRetries" in job
        assert "overlapPolicy" in job


class TestGetJob:
    async def test_found(self, client: AsyncClient, job_ids: dict) -> None:
        resp = await client.get(f"/api/v1/qcli/jobs/{job_ids['quick']}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "quick"

    async def test_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/qcli/jobs/nonexistent")
        assert resp.status_code == 404
        assert resp.json()["code"] == "JOB_NOT_FOUND"


class TestTriggerJob:
    async def test_trigger_success(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        jobs_plugin.scheduler._running = True
        resp = await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/trigger")
        assert resp.status_code == 200
        assert resp.json()["message"] == "Job triggered"
        await asyncio.sleep(0.2)

    async def test_trigger_not_found(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/qcli/jobs/nonexistent/trigger")
        assert resp.status_code == 404


class TestPauseResume:
    async def test_pause(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        jobs_plugin.scheduler._running = True
        resp = await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/pause")
        assert resp.status_code == 200

    async def test_pause_already_paused(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        jobs_plugin.scheduler._running = True
        await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/pause")
        resp = await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/pause")
        assert resp.status_code == 409
        assert resp.json()["code"] == "JOB_ALREADY_PAUSED"

    async def test_resume(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        jobs_plugin.scheduler._running = True
        await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/pause")
        resp = await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/resume")
        assert resp.status_code == 200

    async def test_resume_not_paused(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        jobs_plugin.scheduler._running = True
        resp = await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/resume")
        assert resp.status_code == 409
        assert resp.json()["code"] == "JOB_NOT_PAUSED"


class TestStopJob:
    async def test_stop(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        jobs_plugin.scheduler._running = True
        resp = await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/stop")
        assert resp.status_code == 200

    async def test_stop_not_found(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/qcli/jobs/nonexistent/stop")
        assert resp.status_code == 404


class TestCancelJob:
    async def test_cancel_not_running(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        jobs_plugin.scheduler._running = True
        resp = await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/cancel")
        assert resp.status_code == 409
        assert resp.json()["code"] == "JOB_NOT_RUNNING"


class TestUpdateJob:
    async def test_update_description(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        jobs_plugin.scheduler._running = True
        resp = await client.put(
            f"/api/v1/qcli/jobs/{job_ids['quick']}",
            json={"description": "updated", "maxRetries": 5},
        )
        assert resp.status_code == 200
        reg = jobs_plugin.scheduler.registrations[job_ids["quick"]]
        assert reg.options.description == "updated"
        assert reg.options.max_retries == 5

    async def test_update_invalid_schedule(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        jobs_plugin.scheduler._running = True
        resp = await client.put(
            f"/api/v1/qcli/jobs/{job_ids['quick']}",
            json={"schedule": "not-valid"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "INVALID_SCHEDULE"

    async def test_update_not_found(self, client: AsyncClient) -> None:
        resp = await client.put("/api/v1/qcli/jobs/nonexistent", json={"description": "x"})
        assert resp.status_code == 404


class TestHistory:
    async def test_history_empty(self, client: AsyncClient, job_ids: dict) -> None:
        resp = await client.get(f"/api/v1/qcli/jobs/{job_ids['quick']}/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    async def test_history_after_trigger(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        jobs_plugin.scheduler._running = True
        await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/trigger")
        await asyncio.sleep(0.2)
        resp = await client.get(f"/api/v1/qcli/jobs/{job_ids['quick']}/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert data["items"][0]["status"] == "completed"

    async def test_history_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/qcli/jobs/nonexistent/history")
        assert resp.status_code == 404

    async def test_execution_detail(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        jobs_plugin.scheduler._running = True
        await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/trigger")
        await asyncio.sleep(0.2)
        history = await client.get(f"/api/v1/qcli/jobs/{job_ids['quick']}/history")
        exec_id = history.json()["items"][0]["id"]
        resp = await client.get(
            f"/api/v1/qcli/jobs/{job_ids['quick']}/history/{exec_id}"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "logs" in data
        assert len(data["logs"]) > 0

    async def test_execution_not_found(self, client: AsyncClient, job_ids: dict) -> None:
        resp = await client.get(
            f"/api/v1/qcli/jobs/{job_ids['quick']}/history/nonexistent"
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "EXECUTION_NOT_FOUND"


# ---------------------------------------------------------------------------
# Deleting runs
# ---------------------------------------------------------------------------

from qodalis_cli_server_abstractions.jobs import (  # noqa: E402
    ICliJobStorageProvider,
    JobExecution,
    JobState,
)


async def _add_run(plugin: CliJobsPlugin, job_id: str, exec_id: str, status: str) -> None:
    await plugin.scheduler.storage.save_execution(
        JobExecution(id=exec_id, job_id=job_id, job_name=job_id, status=status)
    )


class _KeepingStorage(ICliJobStorageProvider):
    """A store that cannot delete executions (keeps the base delete_execution)."""

    def __init__(self) -> None:
        self.items: dict[str, JobExecution] = {}

    async def save_execution(self, execution: JobExecution) -> None:
        self.items[execution.id] = execution

    async def get_executions(self, job_id, *, limit=20, offset=0, status=None):
        items = [e for e in self.items.values() if e.job_id == job_id]
        return items[offset: offset + limit], len(items)

    async def get_execution(self, execution_id):
        return self.items.get(execution_id)

    async def save_job_state(self, job_id: str, state: JobState) -> None:
        pass

    async def get_job_state(self, job_id: str):
        return None

    async def get_all_job_states(self):
        return {}


class TestDeleteRun:
    async def test_deletes_run_and_logs(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        quick = job_ids["quick"]
        await _add_run(jobs_plugin, quick, "e1", "completed")
        resp = await client.delete(f"/api/v1/qcli/jobs/{quick}/history/e1")
        assert resp.status_code == 200
        assert resp.json() == {"message": "Run deleted"}
        assert (await client.get(f"/api/v1/qcli/jobs/{quick}/history/e1")).status_code == 404
        assert (await client.get(f"/api/v1/qcli/jobs/{quick}/history")).json()["total"] == 0

    async def test_unknown_job(self, client: AsyncClient) -> None:
        resp = await client.delete("/api/v1/qcli/jobs/nonexistent/history/e1")
        assert resp.status_code == 404
        assert resp.json() == {"error": "Job not found", "code": "JOB_NOT_FOUND"}

    async def test_missing_run(self, client: AsyncClient, job_ids: dict) -> None:
        resp = await client.delete(f"/api/v1/qcli/jobs/{job_ids['quick']}/history/missing")
        assert resp.status_code == 404
        assert resp.json() == {"error": "Execution not found", "code": "EXECUTION_NOT_FOUND"}

    async def test_run_of_another_job(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        await _add_run(jobs_plugin, job_ids["slow"], "e1", "completed")
        resp = await client.delete(f"/api/v1/qcli/jobs/{job_ids['quick']}/history/e1")
        assert resp.status_code == 404
        assert resp.json()["code"] == "EXECUTION_NOT_FOUND"
        assert await jobs_plugin.scheduler.storage.get_execution("e1") is not None

    async def test_running_run(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        await _add_run(jobs_plugin, job_ids["quick"], "e1", "running")
        resp = await client.delete(f"/api/v1/qcli/jobs/{job_ids['quick']}/history/e1")
        assert resp.status_code == 409
        assert resp.json()["code"] == "EXECUTION_RUNNING"
        assert await jobs_plugin.scheduler.storage.get_execution("e1") is not None

    async def test_storage_cannot_delete(self) -> None:
        plugin = (
            CliJobsBuilder()
            .set_storage_provider(_KeepingStorage())
            .add_job(QuickJob(), CliJobOptions(name="quick", interval="999s"))
            .build()
        )
        await _add_run(plugin, "quick", "e1", "completed")
        app = FastAPI()
        app.include_router(plugin.router, prefix="/api/v1/qcli/jobs")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            one = await c.delete("/api/v1/qcli/jobs/quick/history/e1")
            assert one.status_code == 501
            assert one.json()["code"] == "NOT_SUPPORTED"
            bulk = await c.delete("/api/v1/qcli/jobs/quick/history")
            assert bulk.status_code == 501
            assert bulk.json()["code"] == "NOT_SUPPORTED"


class TestDeleteRuns:
    async def _seed(self, plugin: CliJobsPlugin, job_id: str) -> None:
        await _add_run(plugin, job_id, "ok1", "completed")
        await _add_run(plugin, job_id, "ok2", "completed")
        await _add_run(plugin, job_id, "bad1", "failed")
        await _add_run(plugin, job_id, "run1", "running")

    async def test_deletes_every_finished_run(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        quick = job_ids["quick"]
        await self._seed(jobs_plugin, quick)
        await _add_run(jobs_plugin, job_ids["slow"], "other", "completed")
        resp = await client.delete(f"/api/v1/qcli/jobs/{quick}/history")
        assert resp.status_code == 200
        assert resp.json() == {"deleted": 3}
        history = (await client.get(f"/api/v1/qcli/jobs/{quick}/history")).json()
        assert [e["id"] for e in history["items"]] == ["run1"]
        assert await jobs_plugin.scheduler.storage.get_execution("other") is not None

    async def test_deletes_runs_with_status(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        quick = job_ids["quick"]
        await self._seed(jobs_plugin, quick)
        resp = await client.delete(f"/api/v1/qcli/jobs/{quick}/history", params={"status": "failed"})
        assert resp.status_code == 200
        assert resp.json() == {"deleted": 1}
        ids = {e["id"] for e in (await client.get(f"/api/v1/qcli/jobs/{quick}/history")).json()["items"]}
        assert ids == {"ok1", "ok2", "run1"}

    async def test_never_deletes_running_runs(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        quick = job_ids["quick"]
        await self._seed(jobs_plugin, quick)
        resp = await client.delete(f"/api/v1/qcli/jobs/{quick}/history", params={"status": "running"})
        assert resp.json() == {"deleted": 0}

    async def test_pages_through_many_runs(self, client: AsyncClient, job_ids: dict, jobs_plugin: CliJobsPlugin) -> None:
        quick = job_ids["quick"]
        for i in range(250):
            await _add_run(jobs_plugin, quick, f"e{i}", "completed")
        resp = await client.delete(f"/api/v1/qcli/jobs/{quick}/history")
        assert resp.json() == {"deleted": 250}

    async def test_unknown_job(self, client: AsyncClient) -> None:
        resp = await client.delete("/api/v1/qcli/jobs/nonexistent/history")
        assert resp.status_code == 404
        assert resp.json() == {"error": "Job not found", "code": "JOB_NOT_FOUND"}
