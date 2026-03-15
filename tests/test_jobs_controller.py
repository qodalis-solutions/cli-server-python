"""Tests for the jobs REST controller."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from qodalis_cli_server_abstractions.jobs import CliJobOptions, ICliJob, ICliJobExecutionContext

from qodalis_cli import CliServerOptions, create_cli_server


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
def server_result():
    result = create_cli_server(
        CliServerOptions(
            configure=lambda builder: (
                builder
                .add_job(QuickJob(), CliJobOptions(name="quick", interval="999s"))
                .add_job(SlowJob(), CliJobOptions(name="slow", interval="999s"))
            )
        )
    )
    return result


@pytest.fixture()
def job_ids(server_result):
    """Return dict mapping name -> id."""
    return {
        reg.options.name: reg.id
        for reg in server_result.job_scheduler.registrations.values()
    }


@pytest.fixture()
async def client(server_result):
    transport = ASGITransport(app=server_result.app)
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
    async def test_trigger_success(self, client: AsyncClient, job_ids: dict, server_result) -> None:
        server_result.job_scheduler._running = True
        resp = await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/trigger")
        assert resp.status_code == 200
        assert resp.json()["message"] == "Job triggered"
        await asyncio.sleep(0.2)

    async def test_trigger_not_found(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/qcli/jobs/nonexistent/trigger")
        assert resp.status_code == 404


class TestPauseResume:
    async def test_pause(self, client: AsyncClient, job_ids: dict, server_result) -> None:
        server_result.job_scheduler._running = True
        resp = await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/pause")
        assert resp.status_code == 200

    async def test_pause_already_paused(self, client: AsyncClient, job_ids: dict, server_result) -> None:
        server_result.job_scheduler._running = True
        await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/pause")
        resp = await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/pause")
        assert resp.status_code == 409
        assert resp.json()["code"] == "JOB_ALREADY_PAUSED"

    async def test_resume(self, client: AsyncClient, job_ids: dict, server_result) -> None:
        server_result.job_scheduler._running = True
        await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/pause")
        resp = await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/resume")
        assert resp.status_code == 200

    async def test_resume_not_paused(self, client: AsyncClient, job_ids: dict, server_result) -> None:
        server_result.job_scheduler._running = True
        resp = await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/resume")
        assert resp.status_code == 409
        assert resp.json()["code"] == "JOB_NOT_PAUSED"


class TestStopJob:
    async def test_stop(self, client: AsyncClient, job_ids: dict, server_result) -> None:
        server_result.job_scheduler._running = True
        resp = await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/stop")
        assert resp.status_code == 200

    async def test_stop_not_found(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/qcli/jobs/nonexistent/stop")
        assert resp.status_code == 404


class TestCancelJob:
    async def test_cancel_not_running(self, client: AsyncClient, job_ids: dict, server_result) -> None:
        server_result.job_scheduler._running = True
        resp = await client.post(f"/api/v1/qcli/jobs/{job_ids['quick']}/cancel")
        assert resp.status_code == 409
        assert resp.json()["code"] == "JOB_NOT_RUNNING"


class TestUpdateJob:
    async def test_update_description(self, client: AsyncClient, job_ids: dict, server_result) -> None:
        server_result.job_scheduler._running = True
        resp = await client.put(
            f"/api/v1/qcli/jobs/{job_ids['quick']}",
            json={"description": "updated", "maxRetries": 5},
        )
        assert resp.status_code == 200
        reg = server_result.job_scheduler.registrations[job_ids["quick"]]
        assert reg.options.description == "updated"
        assert reg.options.max_retries == 5

    async def test_update_invalid_schedule(self, client: AsyncClient, job_ids: dict, server_result) -> None:
        server_result.job_scheduler._running = True
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

    async def test_history_after_trigger(self, client: AsyncClient, job_ids: dict, server_result) -> None:
        server_result.job_scheduler._running = True
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

    async def test_execution_detail(self, client: AsyncClient, job_ids: dict, server_result) -> None:
        server_result.job_scheduler._running = True
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
