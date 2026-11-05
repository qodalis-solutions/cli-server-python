"""Dashboard insights (1.0.0-beta.19): jobs statistics, log histogram,
status history, command usage and the audit time filter."""

from __future__ import annotations

import asyncio
import datetime
import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli.services.cli_command_usage import CliCommandUsageTracker
from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder
from qodalis_cli_admin.services.log_ring_buffer import LogEntry, LogRingBuffer
from qodalis_cli_admin.services.status_history import MAX_SAMPLES, StatusHistory
from qodalis_cli_audit import CliAuditBuilder, InMemoryAuditStore
from qodalis_cli_jobs import CliJobsBuilder, InMemoryJobStorageProvider
from qodalis_cli_jobs.job_stats import outcome_of
from qodalis_cli_server_abstractions.audit import AuditEntry, AuditQueryOptions
from qodalis_cli_server_abstractions.jobs import (
    CliJobOptions,
    ICliJob,
    ICliJobExecutionContext,
    ICliJobStorageProvider,
    JobExecution,
)

from .conftest import EchoProcessor, FailingProcessor, StreamProcessor

UTC = datetime.timezone.utc
BASE = "/api/v1/qcli"
JOBS = BASE + "/jobs"
DAY = datetime.datetime(2026, 11, 4, tzinfo=UTC)
SINCE = "2026-11-04T00:00:00Z"
UNTIL = "2026-11-05T00:00:00Z"


def at(hours: float) -> datetime.datetime:
    return DAY + datetime.timedelta(hours=hours)


def assert_error(res, status: int = 400) -> None:
    assert res.status_code == status, res.text
    body = res.json()
    assert set(body) == {"error"} and body["error"], body


# =========================================================================
# 1. Jobs statistics
# =========================================================================


class _NoopJob(ICliJob):
    async def execute_async(self, context: ICliJobExecutionContext, cancellation_event: asyncio.Event) -> None:
        return None


class _PerJobOnlyStorage(ICliJobStorageProvider):
    """Only the required methods: the default cross-job query raises without
    job ids, so the scheduler merges per job."""

    def __init__(self) -> None:
        self.inner = InMemoryJobStorageProvider()

    async def save_execution(self, execution):
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


def _run(job_id: str, n: int, status: str, start: datetime.datetime, duration: float | None) -> JobExecution:
    return JobExecution(
        id=f"{job_id}-{n}", job_id=job_id, job_name=f"Job {job_id.upper()}",
        status=status, started_at=start, duration=duration,
    )


SAMPLE_RUNS = [
    _run("a", 1, "completed", at(0.5), 100),
    _run("a", 2, "failed", at(1), 300),  # exactly on a column boundary
    _run("a", 3, "timed_out", at(24) - datetime.timedelta(milliseconds=1), 500),
    _run("a", 4, "running", at(5), None),
    _run("b", 1, "cancelled", at(2), 50),
    _run("b", 2, "Succeeded", at(3), 200),
    _run("b", 3, "Error", at(4), 1000),
    _run("b", 4, "pending", at(6), None),
    _run("b", 5, "weird", at(7), 10),
    # Outside [since, until)
    _run("a", 5, "failed", at(24), 1),
    _run("b", 6, "failed", at(-0.001), 1),
]


async def _jobs_client(storage: ICliJobStorageProvider, runs: list[JobExecution]):
    plugin = (
        CliJobsBuilder()
        .set_storage_provider(storage)
        .add_job(_NoopJob(), CliJobOptions(name="a", group="Reports"))
        .add_job(_NoopJob(), CliJobOptions(name="b", group="other"))
        .build()
    )
    for run in runs:
        await storage.save_execution(run)
    app = FastAPI()
    app.include_router(plugin.router, prefix=plugin.prefix)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(params=["in-memory", "per-job-only"])
async def jobs(request):
    storage = InMemoryJobStorageProvider() if request.param == "in-memory" else _PerJobOnlyStorage()
    async with await _jobs_client(storage, SAMPLE_RUNS) as client:
        yield client


class TestJobsStats:
    @pytest.mark.parametrize(("status", "outcome"), [
        ("completed", "succeeded"), ("SUCCEEDED", "succeeded"),
        ("failed", "failed"), ("error", "failed"), ("timed_out", "failed"), ("TimedOut", "failed"),
        ("running", "running"), ("pending", "running"),
        ("cancelled", "cancelled"), ("skipped", "cancelled"), ("", "cancelled"), (None, "cancelled"),
    ])
    def test_outcome_mapping(self, status, outcome) -> None:
        assert outcome_of(status) == outcome

    async def test_buckets_totals_and_jobs(self, jobs) -> None:
        res = await jobs.get(f"{JOBS}/stats?since={SINCE}&until={UNTIL}")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["since"] == "2026-11-04T00:00:00.000Z"
        assert body["until"] == "2026-11-05T00:00:00.000Z"
        assert body["bucketMs"] == 3_600_000
        assert len(body["buckets"]) == 24
        assert body["buckets"][0] == {
            "start": "2026-11-04T00:00:00.000Z", "succeeded": 1, "failed": 0, "cancelled": 0, "running": 0,
        }
        assert body["buckets"][1]["failed"] == 1 and body["buckets"][1]["start"] == "2026-11-04T01:00:00.000Z"
        assert body["buckets"][23]["failed"] == 1
        assert body["buckets"][2]["cancelled"] == 1
        assert body["buckets"][3]["succeeded"] == 1
        assert body["buckets"][4]["failed"] == 1
        assert body["buckets"][5]["running"] == 1 and body["buckets"][6]["running"] == 1
        assert body["buckets"][7]["cancelled"] == 1

        totals = body["totals"]
        assert totals["total"] == 9
        assert (totals["succeeded"], totals["failed"], totals["cancelled"], totals["running"]) == (2, 3, 2, 2)
        assert totals["successRate"] == pytest.approx(0.4)
        # Finished runs with a duration: 10, 50, 100, 200, 300, 500, 1000
        assert totals["avgDuration"] == pytest.approx(2160 / 7)
        assert totals["p95Duration"] == 1000
        assert totals["maxDuration"] == 1000
        assert totals["lastFailureAt"] == "2026-11-04T23:59:59.999Z"

        assert [j["jobId"] for j in body["jobs"]] == ["a", "b"]
        a, b = body["jobs"]
        assert a["jobName"] == "Job A"
        assert (a["total"], a["succeeded"], a["failed"], a["running"], a["cancelled"]) == (4, 1, 2, 1, 0)
        assert a["successRate"] == pytest.approx(1 / 3)
        assert a["avgDuration"] == pytest.approx(300) and a["p95Duration"] == 500 and a["maxDuration"] == 500
        assert a["lastFailureAt"] == "2026-11-04T23:59:59.999Z"
        # Newest first
        assert [r["status"] for r in a["recent"]] == ["timed_out", "running", "failed", "completed"]
        assert a["recent"][0] == {"status": "timed_out", "startedAt": "2026-11-04T23:59:59.999Z", "duration": 500}
        assert b["lastFailureAt"] == "2026-11-04T04:00:00.000Z"
        assert b["successRate"] == pytest.approx(0.5)

    async def test_job_and_group_filters(self, jobs) -> None:
        async def ids(query: str) -> list[str]:
            res = await jobs.get(f"{JOBS}/stats?since={SINCE}&until={UNTIL}&{query}")
            assert res.status_code == 200, res.text
            return [j["jobId"] for j in res.json()["jobs"]]

        assert await ids("job=a") == ["a"]
        assert await ids("job=a,b") == ["a", "b"]
        assert await ids("job=b&job=a") == ["a", "b"]
        assert await ids("group=REPORTS") == ["a"]
        assert await ids("group=reports&job=b") == []
        assert await ids("job=nope") == []
        # A group without jobs gives empty stats, not every job
        res = (await jobs.get(f"{JOBS}/stats?since={SINCE}&until={UNTIL}&group=none")).json()
        assert res["jobs"] == [] and res["totals"]["total"] == 0
        assert all(sum(v for k, v in c.items() if k != "start") == 0 for c in res["buckets"])

    async def test_empty_range(self, jobs) -> None:
        res = await jobs.get(f"{JOBS}/stats?since=2020-01-01T00:00:00&until=2020-01-02T00:00:00&buckets=4")
        body = res.json()
        assert body["bucketMs"] == 6 * 3_600_000 and len(body["buckets"]) == 4
        assert body["jobs"] == []
        assert body["totals"] == {
            "total": 0, "succeeded": 0, "failed": 0, "cancelled": 0, "running": 0,
            "successRate": None, "avgDuration": None, "p95Duration": None, "maxDuration": None,
            "lastFailureAt": None,
        }

    @pytest.mark.parametrize(("value", "expected"), [("0", 1), ("-3", 1), ("1", 1), ("200", 200), ("5000", 200), ("", 24)])
    async def test_buckets_are_clamped(self, jobs, value, expected) -> None:
        res = await jobs.get(f"{JOBS}/stats?since={SINCE}&until={UNTIL}&buckets={value}")
        assert len(res.json()["buckets"]) == expected

    async def test_defaults_to_the_last_24_hours(self, jobs) -> None:
        body = (await jobs.get(f"{JOBS}/stats")).json()
        since = datetime.datetime.fromisoformat(body["since"].replace("Z", "+00:00"))
        until = datetime.datetime.fromisoformat(body["until"].replace("Z", "+00:00"))
        assert until - since == datetime.timedelta(hours=24)
        assert abs((datetime.datetime.now(UTC) - until).total_seconds()) < 60
        assert len(body["buckets"]) == 24 and body["bucketMs"] == 3_600_000

    async def test_offsets_are_converted_to_utc(self, jobs) -> None:
        body = (await jobs.get(f"{JOBS}/stats?since=2026-11-04T02:00:00%2B02:00&until={UNTIL}")).json()
        assert body["since"] == "2026-11-04T00:00:00.000Z"
        assert body["totals"]["total"] == 9

    @pytest.mark.parametrize("query", [
        "since=yesterday", "until=2026-13-01", "buckets=x", "buckets=1.5",
        f"since={UNTIL}&until={SINCE}", f"since={SINCE}&until={SINCE}",
    ])
    async def test_bad_values_are_400(self, jobs, query) -> None:
        assert_error(await jobs.get(f"{JOBS}/stats?{query}"))

    async def test_not_shadowed_by_job_id(self, jobs) -> None:
        res = await jobs.get(f"{JOBS}/stats")
        assert res.status_code == 200 and "totals" in res.json()
        assert (await jobs.get(f"{JOBS}/a")).json()["id"] == "a"

    @pytest.mark.parametrize("count", [20, 21])
    async def test_p95_is_nearest_rank(self, count) -> None:
        runs = [_run("a", i, "completed", at(1), float(i)) for i in range(1, count + 1)]
        async with await _jobs_client(InMemoryJobStorageProvider(), runs) as client:
            body = (await client.get(f"{JOBS}/stats?since={SINCE}&until={UNTIL}")).json()
        # sorted[ceil(0.95 n) - 1]: 20 -> 19th, 21 -> 20th
        assert body["totals"]["p95Duration"] == (19 if count == 20 else 20)
        assert body["totals"]["maxDuration"] == count

    async def test_reads_every_run_without_a_cap(self) -> None:
        runs = [
            _run("a" if i % 2 else "b", i, "failed" if i % 10 == 0 else "completed",
                 at(i * 23 / 1234), 5)
            for i in range(1234)
        ]
        async with await _jobs_client(InMemoryJobStorageProvider(), runs) as client:
            body = (await client.get(f"{JOBS}/stats?since={SINCE}&until={UNTIL}")).json()
        assert body["totals"]["total"] == 1234
        assert body["totals"]["failed"] == 124
        assert sum(c["succeeded"] + c["failed"] for c in body["buckets"]) == 1234
        assert len(body["jobs"][0]["recent"]) == 12


# =========================================================================
# 2. Log histogram
# =========================================================================


def _entry(when: datetime.datetime, level: str, message: str = "m", source: str = "app") -> LogEntry:
    return LogEntry(timestamp=when.isoformat(), level=level, message=message, logger_name=source)


@pytest.fixture()
def log_buffer() -> LogRingBuffer:
    buffer = LogRingBuffer(max_size=100)
    for entry in (
        _entry(at(0), "DEBUG"),
        _entry(at(0.5), "INFO", "user signed in"),
        _entry(at(1), "INFO", source="qodalis.users"),
        _entry(at(1.5), "WARNING", "disk almost full"),
        _entry(at(2), "ERROR", "boom"),
        _entry(at(2.5), "CRITICAL", "BOOM"),
        _entry(at(23.99), "INFO"),
        _entry(at(24), "ERROR"),  # at "to": excluded
        _entry(at(-1), "ERROR"),  # before "from": excluded
    ):
        buffer.add(entry)
    return buffer


class TestLogHistogram:
    def test_counts_per_level_and_column(self, log_buffer) -> None:
        body = log_buffer.histogram(at(0), at(24), 24)
        assert body["from"] == "2026-11-04T00:00:00.000Z" and body["to"] == "2026-11-05T00:00:00.000Z"
        assert body["bucketMs"] == 3_600_000 and body["store"] == "memory"
        assert body["buckets"][0] == {"start": "2026-11-04T00:00:00.000Z", "DEBUG": 1, "INFO": 1, "WARN": 0, "ERROR": 0}
        assert body["buckets"][1] == {"start": "2026-11-04T01:00:00.000Z", "DEBUG": 0, "INFO": 1, "WARN": 1, "ERROR": 0}
        assert body["buckets"][2]["ERROR"] == 2  # CRITICAL counts as ERROR
        assert body["buckets"][23]["INFO"] == 1
        assert body["totals"] == {"DEBUG": 1, "INFO": 3, "WARN": 1, "ERROR": 2}

    def test_filters(self, log_buffer) -> None:
        assert log_buffer.histogram(at(0), at(24), 1, levels=["WARN", "ERROR"])["totals"] == {
            "DEBUG": 0, "INFO": 0, "WARN": 1, "ERROR": 2,
        }
        # Literal, case-insensitive, in the message or the source
        assert log_buffer.histogram(at(0), at(24), 1, search="BOOM")["totals"]["ERROR"] == 2
        assert log_buffer.histogram(at(0), at(24), 1, search="USER")["totals"]["INFO"] == 2
        assert log_buffer.histogram(at(0), at(24), 1, search="[a-z]+")["totals"] == {
            "DEBUG": 0, "INFO": 0, "WARN": 0, "ERROR": 0,
        }
        # The in-memory buffer has no services
        assert sum(log_buffer.histogram(at(0), at(24), 1, service="api")["totals"].values()) == 0

    def test_logs_endpoint_accepts_the_same_filters(self, log_buffer) -> None:
        items, total = log_buffer.query(level="warn,ERROR")
        assert total == 4 and {i["level"] for i in items} == {"WARN", "ERROR"}
        assert log_buffer.query(search="qodalis.USERS")[1] == 1
        assert log_buffer.query(service="api")[1] == 0

    async def test_endpoint(self, log_buffer) -> None:
        server = create_cli_server(CliServerOptions())
        plugin = CliAdminBuilder().set_credentials("admin", "pass").set_jwt_secret("x" * 40).build(AdminBuildDeps(
            registry=server.registry, event_socket_manager=server.event_socket_manager, builder=server.builder,
        ))
        for entry in list(log_buffer._buffer):
            plugin.log_buffer.add(entry)
        now = datetime.datetime.now(UTC)
        plugin.log_buffer.add(_entry(now - datetime.timedelta(minutes=5), "WARNING", "recent"))
        app = FastAPI()
        app.include_router(plugin.router, prefix=BASE)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get(f"{BASE}/logs/histogram")).status_code == 401
            token = (await client.post(f"{BASE}/auth/login", json={"username": "admin", "password": "pass"})).json()["token"]
            h = {"Authorization": f"Bearer {token}"}

            body = (await client.get(f"{BASE}/logs/histogram", headers=h)).json()
            assert len(body["buckets"]) == 24 and body["bucketMs"] == 3_600_000
            assert body["totals"]["WARN"] >= 1

            body = (await client.get(
                f"{BASE}/logs/histogram?from={SINCE}&to={UNTIL}&buckets=2&level=info,ERROR", headers=h,
            )).json()
            assert body["bucketMs"] == 12 * 3_600_000
            assert body["totals"] == {"DEBUG": 0, "INFO": 3, "WARN": 0, "ERROR": 2}
            assert body["buckets"][0]["INFO"] == 2 and body["buckets"][1]["INFO"] == 1

            body = (await client.get(f"{BASE}/logs/histogram?from={SINCE}&to={UNTIL}&buckets=999", headers=h)).json()
            assert len(body["buckets"]) == 200

            for query in ("from=nope", "to=2026-02-30", "buckets=x", "level=LOUD", f"from={UNTIL}&to={SINCE}"):
                assert_error(await client.get(f"{BASE}/logs/histogram?{query}", headers=h))

            logs = (await client.get(f"{BASE}/logs?level=WARN,ERROR", headers=h)).json()
            assert logs["total"] == 5

            # GET /logs answers like the .NET server, so the dashboard filters on the server
            logs = (await client.get(f"{BASE}/logs?limit=3", headers=h)).json()
            assert set(logs) == {"entries", "total", "limit", "offset", "hasMore", "store", "persistent", "services"}
            assert logs["total"] == 10 and len(logs["entries"]) == 3 and logs["hasMore"] is True
            assert logs["store"] == "memory" and logs["persistent"] is False and logs["services"] == []
            last = (await client.get(f"{BASE}/logs?limit=3&offset=8", headers=h)).json()
            assert len(last["entries"]) == 2 and last["hasMore"] is False and last["offset"] == 8

            async def total(query: str) -> int:
                res = await client.get(f"{BASE}/logs?{query}", headers=h)
                assert res.status_code == 200, res.text
                return res.json()["total"]

            assert await total(f"from={SINCE}&to={UNTIL}") == 7
            # No offset = UTC; from inclusive, to exclusive
            assert await total("from=2026-11-04T01:00:00&to=2026-11-04T02:00:00Z") == 2
            assert await total("to=2026-11-04T03:00:00%2B02:00") == 3  # 01:00Z: entries at -1h, 0h, 0.5h
            assert await total("from=2026-11-04T01:00:00Z&level=error") == 2
            for query in ("from=nope", "to=2026-02-30"):
                assert_error(await client.get(f"{BASE}/logs?{query}", headers=h))


# =========================================================================
# 3. Status history
# =========================================================================


class _Clock:
    def __init__(self) -> None:
        self.wall = 100.0
        self.cpu = 10.0

    def advance(self, wall: float, cpu: float) -> None:
        self.wall += wall
        self.cpu += cpu


class _Sockets:
    def get_clients(self):
        return [1, 2]


class TestStatusHistory:
    def _history(self, clock: _Clock, **kwargs) -> StatusHistory:
        return StatusHistory(
            _Sockets(), clock=lambda: clock.wall, cpu_clock=lambda: clock.cpu, memory=lambda: 254.0, **kwargs,
        )

    def test_cpu_percent_from_process_time_deltas(self) -> None:
        clock = _Clock()
        history = self._history(clock)
        first = history.sample()
        assert first["cpuPercent"] is None
        assert first["memoryMb"] == 254.0 and first["activeWsConnections"] == 2 and first["activeShellSessions"] == 0
        clock.advance(15, 0.48)
        assert history.sample()["cpuPercent"] == pytest.approx(3.2)
        clock.advance(10, 20)  # two cores busy
        assert history.sample()["cpuPercent"] == pytest.approx(200)

    def test_cpu_is_null_when_the_platform_cannot_tell(self) -> None:
        def broken() -> float:
            raise OSError("no cpu times")

        history = StatusHistory(None, cpu_clock=broken, memory=lambda: 1.0)
        history.sample()
        sample = history.sample()
        assert sample["cpuPercent"] is None and sample["activeWsConnections"] == 0

    def test_ring_buffer_keeps_24_hours(self) -> None:
        assert MAX_SAMPLES == 5760
        clock = _Clock()
        history = self._history(clock, max_samples=5)
        base = datetime.datetime.now(UTC) - datetime.timedelta(minutes=10)
        for i in range(8):
            history.sample(now=base + datetime.timedelta(seconds=15 * i))
        assert len(history.samples) == 5
        assert history.samples[0]["timestamp"] == base + datetime.timedelta(seconds=45)

    def test_history_window_is_oldest_first(self) -> None:
        clock = _Clock()
        history = self._history(clock)
        now = datetime.datetime.now(UTC)
        for minutes in (120, 59, 30, 1):
            history.sample(now=now - datetime.timedelta(minutes=minutes))
        samples = history.history(60, now=now)
        assert len(samples) == 3
        assert samples[0]["timestamp"] < samples[1]["timestamp"]
        assert samples[0]["timestamp"].endswith("Z") and len(samples[0]["timestamp"]) == 24
        assert len(history.history(1440, now=now)) == 4

    async def test_sampler_samples_at_start_and_stops(self) -> None:
        history = StatusHistory(None, interval=0.01, memory=lambda: 1.0)
        history.start()
        await asyncio.sleep(0.05)
        assert history.running and len(history.samples) >= 2
        await history.stop()
        assert not history.running
        count = len(history.samples)
        await asyncio.sleep(0.03)
        assert len(history.samples) == count

    def test_runs_with_the_app_lifespan(self) -> None:
        result = create_cli_server(CliServerOptions())
        admin = CliAdminBuilder().set_credentials("admin", "pass").set_jwt_secret("y" * 40).build(AdminBuildDeps(
            registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder,
        ))
        result.mount_plugin(admin)
        history = admin.status_history
        assert history is not None and not history.running
        with TestClient(result.app) as client:
            assert history.running
            token = client.post(f"{BASE}/auth/login", json={"username": "admin", "password": "pass"}).json()["token"]
            h = {"Authorization": f"Bearer {token}"}
            body = client.get(f"{BASE}/status/history", headers=h).json()
            assert body["intervalSeconds"] == 15
            assert len(body["samples"]) == 1  # first sample at start
            sample = body["samples"][0]
            assert set(sample) == {"timestamp", "memoryMb", "cpuPercent", "activeWsConnections", "activeShellSessions"}
            assert sample["cpuPercent"] is None and sample["memoryMb"] > 0

            for minutes in ("0", "-5", "5000", "1", ""):
                assert client.get(f"{BASE}/status/history?minutes={minutes}", headers=h).status_code == 200
            assert_error(client.get(f"{BASE}/status/history?minutes=abc", headers=h))
            assert client.get(f"{BASE}/status/history").status_code == 401
        # Cancelled on shutdown
        assert not history.running

    def test_minutes_are_clamped(self) -> None:
        result = create_cli_server(CliServerOptions())
        admin = CliAdminBuilder().set_credentials("admin", "pass").set_jwt_secret("y" * 40).build(AdminBuildDeps(
            registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder,
        ))
        result.mount_plugin(admin)
        now = datetime.datetime.now(UTC)
        for minutes in (2000, 1000, 30, 0.5):
            admin.status_history.samples.append({
                "timestamp": now - datetime.timedelta(minutes=minutes), "memoryMb": 1.0, "cpuPercent": None,
                "activeWsConnections": 0, "activeShellSessions": 0,
            })
        with TestClient(result.app) as client:
            token = client.post(f"{BASE}/auth/login", json={"username": "admin", "password": "pass"}).json()["token"]
            h = {"Authorization": f"Bearer {token}"}

            def count(query: str) -> int:
                return len(client.get(f"{BASE}/status/history{query}", headers=h).json()["samples"])

            # +1: the sample taken at start
            assert count("") == 3  # default 60 minutes
            assert count("?minutes=0") == 2  # clamped to 1
            assert count("?minutes=99999") == 4  # clamped to 1440
            assert count("?minutes=1440") == 4


# =========================================================================
# 4. Command usage
# =========================================================================


def _server_client() -> TestClient:
    def configure(builder):
        builder.add_processor(EchoProcessor())
        builder.add_processor(FailingProcessor())
        builder.add_processor(StreamProcessor())

    return TestClient(create_cli_server(CliServerOptions(configure=configure)).app)


class TestCommandUsage:
    def test_tracker_sorts_and_counts(self) -> None:
        tracker = CliCommandUsageTracker()
        tracker.record("Echo", known=True, duration_ms=2, failed=False)
        tracker.record("echo", known=True, duration_ms=5, failed=True)
        tracker.record("zz", known=False, duration_ms=1, failed=True)
        tracker.record("aa", known=True, duration_ms=1, failed=False)
        tracker.record("", known=False, duration_ms=1, failed=False)
        body = tracker.snapshot()
        assert [c["command"] for c in body["commands"]] == ["echo", "aa", "zz"]
        echo = body["commands"][0]
        assert echo["executions"] == 2 and echo["failures"] == 1 and echo["avgDuration"] == pytest.approx(3.5)
        assert echo["lastFailedAt"] is not None and echo["known"] is True
        assert body["commands"][1]["lastFailedAt"] is None
        assert body["commands"][2]["known"] is False

    def test_tracker_is_thread_safe(self) -> None:
        tracker = CliCommandUsageTracker()

        def work() -> None:
            for _ in range(1000):
                tracker.record("echo", known=True, duration_ms=1, failed=False)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert tracker.snapshot()["commands"][0]["executions"] == 8000

    def test_counts_execute_and_stream(self) -> None:
        with _server_client() as client:
            empty = client.get(f"{BASE}/commands/stats").json()
            assert empty["commands"] == [] and empty["since"].endswith("Z")

            for body in (
                {"command": "echo", "value": "hi"},
                {"command": "ECHO", "value": "hi"},
                {"command": "fail"},
                {"command": "nope"},
            ):
                assert client.post(f"{BASE}/execute", json=body).status_code == 200
            for body in ({"command": "stream-test"}, {"command": "echo", "value": "x"}, {"command": "nope"}):
                res = client.post(f"{BASE}/execute/stream", json=body)
                assert res.status_code == 200
                res.read()

            stats = {c["command"]: c for c in client.get(f"{BASE}/commands/stats").json()["commands"]}
            assert stats["echo"]["executions"] == 3 and stats["echo"]["failures"] == 0
            assert stats["echo"]["known"] is True and stats["echo"]["lastFailedAt"] is None
            assert stats["fail"]["executions"] == 1 and stats["fail"]["failures"] == 1
            assert stats["fail"]["lastFailedAt"] is not None
            assert stats["nope"] == {**stats["nope"], "known": False, "executions": 2, "failures": 2}
            assert stats["stream-test"]["executions"] == 1 and stats["stream-test"]["failures"] == 0
            assert stats["stream-test"]["avgDuration"] >= 0
            order = [c["command"] for c in client.get(f"{BASE}/commands/stats").json()["commands"]]
            assert order == ["echo", "nope", "fail", "stream-test"]

    def test_not_taken_by_a_command_named_stats(self) -> None:
        with _server_client() as client:
            commands = client.get(f"{BASE}/commands").json()
            assert isinstance(commands, list) and commands
            body = client.get(f"{BASE}/commands/stats").json()
            assert set(body) == {"since", "commands"}

    def test_mcp_tools_are_counted(self) -> None:
        def configure(builder):
            builder.add_processor(EchoProcessor())

        result = create_cli_server(CliServerOptions(configure=configure, mcp=True))
        with TestClient(result.app) as client:
            headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
            client.post("/api/qcli/mcp", headers=headers, content=json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
            }))
            tools = client.post("/api/qcli/mcp", headers=headers, content=json.dumps({
                "jsonrpc": "2.0", "id": 2, "method": "tools/list",
            })).json()["result"]["tools"]
            echo = next(t["name"] for t in tools if "echo" in t["name"])
            client.post("/api/qcli/mcp", headers=headers, content=json.dumps({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": echo, "arguments": {}},
            }))
            stats = client.get(f"{BASE}/commands/stats").json()["commands"]
            assert stats and stats[0]["command"] == "echo" and stats[0]["executions"] == 1


# =========================================================================
# 5. Audit time filter
# =========================================================================


class TestAuditTimeFilter:
    async def test_store_filters_from_inclusive_to_exclusive(self) -> None:
        store = InMemoryAuditStore()
        for ts in (1000, 2000, 3000, 4000):
            await store.log(AuditEntry(event="e", username=str(ts), timestamp=ts))
        names = lambda entries: [e.username for e in entries]  # noqa: E731
        assert names(await store.get_entries(AuditQueryOptions(from_=2000, to=4000))) == ["3000", "2000"]
        assert names(await store.get_entries(AuditQueryOptions(from_=3000))) == ["4000", "3000"]
        assert names(await store.get_entries(AuditQueryOptions(to=2000))) == ["1000"]
        assert names(await store.get_entries(AuditQueryOptions(from_=1000, to=5000, limit=1, offset=1))) == ["3000"]

    def test_api_from_to_iso_and_epoch(self) -> None:
        result = create_cli_server(CliServerOptions())
        audit = CliAuditBuilder().build()
        result.mount_plugin(audit)
        start = int(DAY.timestamp() * 1000)
        hour = 3_600_000
        with TestClient(result.app) as client:
            for i, event in enumerate(("a", "b", "c", "d")):
                client.portal.call(audit.store.log, AuditEntry(event=event, timestamp=start + i * hour))

            def events(query: str) -> list[str]:
                res = client.get(f"{BASE}/audit?{query}")
                assert res.status_code == 200, res.text
                return [e["event"] for e in res.json()]

            assert events(f"from={start + hour}&to={start + 3 * hour}") == ["c", "b"]
            assert events("from=2026-11-04T01:00:00Z&to=2026-11-04T03:00:00Z") == ["c", "b"]
            assert events("from=2026-11-04T01:00:00") == ["d", "c", "b"]  # no offset = UTC
            assert events("to=2026-11-04T03:00:00%2B02:00") == ["a"]  # 01:00Z, exclusive
            assert events("from=2026-11-04T01:00:00Z&event=D") == ["d"]
            assert events("from=2026-11-04T01:00:00Z&limit=1&offset=1") == ["c"]
            for query in ("from=yesterday", "to=12abc"):
                assert_error(client.get(f"{BASE}/audit?{query}"))
