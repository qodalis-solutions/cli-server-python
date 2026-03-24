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

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `name` | `str \| None` | Class name | Job display name |
| `description` | `str \| None` | Name | Human-readable description |
| `group` | `str \| None` | `None` | Logical grouping |
| `schedule` | `str \| None` | `None` | Cron expression (5-field) |
| `interval` | `str \| None` | `None` | Fixed interval (`30s`, `5m`, `1h`, `1d`) |
| `enabled` | `bool` | `True` | Whether the job starts active |
| `max_retries` | `int` | `0` | Retry count on failure |
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

## License

MIT
