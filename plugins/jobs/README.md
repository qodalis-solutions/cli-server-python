# qodalis-cli-jobs

Background job scheduling plugin for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Provides cron and interval-based job scheduling, execution history, retry policies, and a REST API for management.

## Install

```bash
pip install qodalis-cli-jobs
```

## Quick Start

1. Implement `ICliJob`:

```python
from qodalis_cli_server_abstractions.jobs import ICliJob, ICliJobExecutionContext
import asyncio

class HealthCheckJob(ICliJob):
    async def execute_async(
        self, context: ICliJobExecutionContext, cancellation_event: asyncio.Event
    ) -> None:
        context.logger.info("Running health check...")
        # your logic here
        context.logger.info("Health check passed")
```

2. Build the plugin and mount it:

```python
from qodalis_cli_server_abstractions.jobs import CliJobOptions
from qodalis_cli_jobs import CliJobsBuilder

jobs_plugin = (
    CliJobsBuilder()
    .add_job(
        HealthCheckJob(),
        CliJobOptions(
            name="health-check",
            description="Periodic health check",
            group="monitoring",
            interval="30s",
        ),
    )
    .build(broadcast_fn=event_socket_manager.broadcast_message)
)

# Using create_cli_server:
result.mount_plugin(jobs_plugin)

# Or on an existing FastAPI app:
app.include_router(jobs_plugin.router, prefix=jobs_plugin.prefix)

await jobs_plugin.scheduler.start()
```

### Inside a job

The `ICliJobExecutionContext` gives a run:

- `logger`: the run's log lines (`info`, `warning`, ...), kept with the execution.
- `properties`: the run's own values, saved with the execution (whatever its outcome) and returned by the API, e.g. `context.properties["items"] = 32`. Values must serialize to JSON.
- `discard()`: leaves a completed run out of the history, for a frequent job whose run found nothing to do. Failed runs are always kept; the job's last run is still updated.

```python
class SyncOrdersJob(ICliJob):
    async def execute_async(self, context, cancellation_event):
        synced = await sync_orders()
        if synced == 0:
            context.discard()
            return
        context.properties["synced"] = synced
```

To run a job from your own code (e.g. when something happened), use the plugin's scheduler (`ICliJobScheduler`):
`await jobs_plugin.scheduler.trigger("sync-orders")`; `scheduler.jobs` lists the registrations with their status.
Python has no service container, so there is no per-run service scope (the .NET `context.Services`).

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | `str \| None` | Name (`name-2`, ... for duplicates) | Stable id in the API, the history and the saved state; an explicit duplicate raises |
| `name` | `str \| None` | Class name | Job display name |
| `description` | `str \| None` | Name | Human-readable description |
| `group` | `str \| None` | `None` | Logical grouping |
| `schedule` | `str \| None` | `None` | Cron expression (5-field) |
| `interval` | `str \| None` | `None` | Fixed interval (`30s`, `5m`, `1h`, `1d`); across restarts and pauses the next run is the last run plus the interval (right away when that has passed) |
| `run_on_start` | `bool` | `False` | Run once right after the scheduler starts (active jobs only) |
| `enabled` | `bool` | `True` | Whether the job starts active. `False` keeps it stopped whatever state was saved; otherwise a saved pause or stop (made from the API) is kept across restarts |
| `max_retries` | `int` | `1` | Retry count on failure |
| `timeout` | `str \| None` | `None` | Max execution duration (same format as interval) |
| `overlap_policy` | `str` | `skip` | `skip`, `queue`, or `cancel` |

## REST API

All endpoints are mounted at the plugin's built-in prefix (`/api/v1/qcli/jobs`).

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | List all jobs |
| GET | `/{id}` | Get job details |
| POST | `/{id}/trigger` | Trigger immediate execution |
| POST | `/{id}/pause` | Pause scheduled execution |
| POST | `/{id}/resume` | Resume a paused job |
| POST | `/{id}/stop` | Stop job and cancel if running |
| POST | `/{id}/cancel` | Cancel current execution only |
| PUT | `/{id}` | Update job options |
| GET | `/{id}/history` | Paginated execution history |
| GET | `/{id}/history/{exec_id}` | Execution detail with logs |
| DELETE | `/{id}/history/{exec_id}` | Delete one finished run and its logs |
| DELETE | `/{id}/history` | Delete every finished run of the job: `?status=` deletes only the runs with that status (e.g. `failed`) |
| GET | `/executions` | Executions across jobs, newest first: `?job=a,b&group=&status=&since=&limit=&offset=` |
| GET | `/stats` | Run statistics for charts: `?since=&until=&buckets=&job=&group=` (default the last 24 h in 24 columns) |

`job` may be repeated or comma separated; `group` selects the jobs of that group (case-insensitive, intersected with
`job`); `since` is an ISO 8601 time; `limit` is clamped to 1..100. Executions carry `properties`, the job's own
values. `GET /stats` counts every run started in `[since, until)` (it pages through the storage's execution query, so
any storage works) per time column by outcome (`succeeded`, `failed`, `cancelled`, `running`), with totals and per-job
figures (`successRate`, `avgDuration`, `p95Duration`, `maxDuration`, `lastFailureAt`, the 12 newest runs); `buckets` is
clamped to 1..200 and a bad time answers `400 { "error": "..." }`.

`DELETE /{id}/history/{exec_id}` answers `200 { "message": "Run deleted" }`, `404 { "error", "code": "JOB_NOT_FOUND" }`
for an unknown job, `404 { "error", "code": "EXECUTION_NOT_FOUND" }` for a run that is missing or belongs to another
job, `409 { "error", "code": "EXECUTION_RUNNING" }` for a running run and `501 { "error", "code": "NOT_SUPPORTED" }`
when the storage cannot delete runs. `DELETE /{id}/history` never deletes running runs and answers `200 { "deleted": n }`,
with the same 404 and 501 answers. From code: `await scheduler.delete_execution(job_id, exec_id)` and
`await scheduler.delete_executions(job_id, status=None)` (returns the count). Keep job ids stable (`id`): saved pauses and history are found again by id after a restart.

## MCP tools

When mounted with `result.mount_plugin(jobs_plugin)` on a server with the MCP endpoint enabled, the plugin adds the `jobs_list`, `jobs_get {id}`, `jobs_history {id, limit?}` (`jobs:read`) and `jobs_trigger`, `jobs_pause`, `jobs_resume`, `jobs_stop`, `jobs_cancel` `{id}` and `jobs_delete_runs {id, execution?, status?}` (`jobs:manage`) tools. They use the same scheduler as the REST API. See the server README, *MCP endpoint*.

## Custom Storage

By default, execution history is stored in memory. Provide a custom `ICliJobStorageProvider` for persistence:

```python
plugin = (
    CliJobsBuilder()
    .set_storage_provider(MyDatabaseStorageProvider())
    .add_job(HealthCheckJob(), CliJobOptions(name="health-check", interval="30s"))
    .build()
)
```

Besides saving and reading executions and states, a store may implement `query_executions(query)` (executions
across jobs; the default merges `get_executions` per job and raises `NotImplementedError` for every job, where the
scheduler merges the registered jobs instead), `delete_execution(id)` (deletes the run and its logs, for `discard()` and the `DELETE .../history` routes; the
default keeps the run, and the routes then answer `501 NOT_SUPPORTED`)
and `prune(before)` (delete executions older than `before`; the default keeps everything). Call `prune` on a
schedule, for example from a job, to keep the history bounded.

## License

MIT
