"""FastAPI router for the background jobs API."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from qodalis_cli_server_abstractions.jobs import (
    ICliJobStorageProvider,
    JobExecution,
    JobExecutionQuery,
)

from .cli_job_scheduler import (
    CliJobScheduler,
    ExecutionNotFoundError,
    InvalidOperationError,
    NotSupportedError,
)
from .interval_parser import parse_interval
from .job_stats import (
    StatsRequestError,
    collect_runs,
    compute_stats,
    parse_buckets,
    resolve_range,
)


class UpdateJobRequest(BaseModel):
    """Request body for updating job options (patch semantics)."""

    description: str | None = None
    group: str | None = None
    schedule: str | None = None
    interval: str | None = None
    max_retries: int | None = Field(None, alias="maxRetries")
    retry_delay: str | None = Field(None, alias="retryDelay")
    retry_strategy: str | None = Field(None, alias="retryStrategy")
    timeout: str | None = None
    overlap_policy: str | None = Field(None, alias="overlapPolicy")

    model_config = {"populate_by_name": True}


def _serialize_dt(dt: datetime.datetime | None) -> str | None:
    """Serialize a datetime to ISO 8601 with a trailing ``Z`` suffix."""
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z") if dt.tzinfo else dt.isoformat() + "Z"


def _job_dto(reg: Any) -> dict[str, Any]:
    """Convert a job registration to a JSON-serialisable DTO."""
    return {
        "id": reg.id,
        "name": reg.options.name,
        "description": reg.options.description,
        "group": reg.options.group,
        "status": reg.status,
        "schedule": reg.options.schedule,
        "interval": reg.options.interval,
        "enabled": reg.status != "stopped",
        "maxRetries": reg.options.max_retries,
        "retryDelay": reg.options.retry_delay,
        "retryStrategy": reg.options.retry_strategy,
        "timeout": reg.options.timeout,
        "overlapPolicy": reg.options.overlap_policy,
        "currentExecutionId": reg.current_execution_id,
        "nextRunAt": _serialize_dt(reg.next_run_at),
        "lastRunAt": _serialize_dt(reg.last_run_at),
        "lastRunStatus": reg.last_run_status,
        "lastRunDuration": reg.last_run_duration,
    }


def _execution_item(e: JobExecution) -> dict[str, Any]:
    """An execution as listed by the history and the cross-job listing."""
    return {
        "id": e.id,
        "jobId": e.job_id,
        "jobName": e.job_name,
        "status": e.status,
        "startedAt": _serialize_dt(e.started_at),
        "completedAt": _serialize_dt(e.completed_at),
        "duration": e.duration,
        "error": e.error,
        "retryAttempt": e.retry_attempt,
        "logCount": len(e.logs),
        "properties": e.properties,
    }


def create_cli_jobs_router(
    scheduler: CliJobScheduler,
    storage: ICliJobStorageProvider,
) -> APIRouter:
    """Create a FastAPI router with CRUD and control endpoints for jobs."""
    router = APIRouter()

    def _resolve_job_ids(job: list[str] | None, group: str | None) -> list[str] | None:
        """The jobs named in ``job`` (repeated or comma-separated) and/or in
        ``group`` (case-insensitive); ``None`` for every job, ``[]`` for none."""
        job_ids: list[str] | None = None
        if job is not None:
            job_ids = [j.strip() for value in job for j in value.split(",") if j.strip()]
        if group is not None and group.strip():
            in_group = [
                reg.id for reg in scheduler.registrations.values()
                if (reg.options.group or "").lower() == group.lower()
            ]
            job_ids = [j for j in job_ids if j in in_group] if job_ids else in_group
        return job_ids if job_ids or group is not None else None

    @router.get("")
    async def list_jobs() -> list[dict[str, Any]]:
        return [_job_dto(reg) for reg in scheduler.registrations.values()]

    @router.get("/executions")
    async def list_executions(
        job: list[str] | None = Query(None),
        group: str | None = Query(None),
        status: str | None = Query(None),
        since: datetime.datetime | None = Query(None),
        limit: int = Query(20),
        offset: int = Query(0),
    ) -> Any:
        """Executions across jobs, newest first: every job, the jobs named in
        ``job`` (repeat it or separate ids with commas), or the jobs of a
        ``group``; optionally only those started ``since`` a time (ISO 8601)."""
        job_ids = _resolve_job_ids(job, group)
        query = JobExecutionQuery(
            job_ids=job_ids,
            status=status or None,
            since=since,
            limit=min(max(limit, 1), 100),
            offset=max(offset, 0),
        )
        # A group (or job list) without jobs lists nothing, not every job
        if query.job_ids is not None and not query.job_ids:
            items, total = [], 0
        else:
            items, total = await scheduler.query_executions(query)
        return {
            "items": [_execution_item(e) for e in items],
            "total": total,
            "limit": query.limit,
            "offset": query.offset,
        }

    @router.get("/stats")
    async def get_stats(
        job: list[str] | None = Query(None),
        group: str | None = Query(None),
        since: str | None = Query(None),
        until: str | None = Query(None),
        buckets: str | None = Query(None),
    ) -> Any:
        """Run statistics for the dashboard's charts: runs started in
        ``[since, until)`` (default: the last 24 hours) per time column by
        outcome, totals and per-job figures.  ``job``/``group`` filter as in
        ``GET /executions``.  Declared before ``/{job_id}``."""
        try:
            start, end = resolve_range(since, until)
            count = parse_buckets(buckets)
        except StatsRequestError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        job_ids = _resolve_job_ids(job, group)
        # A group (or job list) without jobs gives empty stats, not every job
        runs = [] if job_ids is not None and not job_ids else await collect_runs(
            scheduler.query_executions, job_ids, start, end,
        )
        names = {reg.id: reg.options.name for reg in scheduler.registrations.values()}
        return compute_stats(runs, start, end, count, names)

    @router.get("/{job_id}")
    async def get_job(job_id: str) -> Any:
        reg = scheduler.registrations.get(job_id)
        if reg is None:
            return JSONResponse(
                status_code=404,
                content={"error": "Job not found", "code": "JOB_NOT_FOUND"},
            )
        return _job_dto(reg)

    @router.post("/{job_id}/trigger")
    async def trigger_job(job_id: str) -> Any:
        try:
            await scheduler.trigger(job_id)
            return {"message": "Job triggered"}
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"error": "Job not found", "code": "JOB_NOT_FOUND"},
            )
        except InvalidOperationError as exc:
            return JSONResponse(
                status_code=409,
                content={"error": str(exc), "code": "JOB_ALREADY_RUNNING"},
            )

    @router.post("/{job_id}/pause")
    async def pause_job(job_id: str) -> Any:
        try:
            await scheduler.pause(job_id)
            return {"message": "Job paused"}
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"error": "Job not found", "code": "JOB_NOT_FOUND"},
            )
        except InvalidOperationError as exc:
            return JSONResponse(
                status_code=409,
                content={"error": str(exc), "code": "JOB_ALREADY_PAUSED"},
            )

    @router.post("/{job_id}/resume")
    async def resume_job(job_id: str) -> Any:
        try:
            await scheduler.resume(job_id)
            return {"message": "Job resumed"}
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"error": "Job not found", "code": "JOB_NOT_FOUND"},
            )
        except InvalidOperationError as exc:
            return JSONResponse(
                status_code=409,
                content={"error": str(exc), "code": "JOB_NOT_PAUSED"},
            )

    @router.post("/{job_id}/stop")
    async def stop_job(job_id: str) -> Any:
        try:
            await scheduler.stop_job(job_id)
            return {"message": "Job stopped"}
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"error": "Job not found", "code": "JOB_NOT_FOUND"},
            )

    @router.post("/{job_id}/cancel")
    async def cancel_job(job_id: str) -> Any:
        try:
            await scheduler.cancel_current(job_id)
            return {"message": "Execution cancelled"}
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"error": "Job not found", "code": "JOB_NOT_FOUND"},
            )
        except InvalidOperationError as exc:
            return JSONResponse(
                status_code=409,
                content={"error": str(exc), "code": "JOB_NOT_RUNNING"},
            )

    @router.put("/{job_id}")
    async def update_job(job_id: str, body: UpdateJobRequest) -> Any:
        try:
            await scheduler.update_options(
                job_id,
                description=body.description,
                group=body.group,
                schedule=body.schedule,
                interval=body.interval,
                max_retries=body.max_retries,
                retry_delay=body.retry_delay,
                retry_strategy=body.retry_strategy,
                timeout=body.timeout,
                overlap_policy=body.overlap_policy,
            )
            return {"message": "Job updated"}
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"error": "Job not found", "code": "JOB_NOT_FOUND"},
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": str(exc), "code": "INVALID_SCHEDULE"},
            )

    @router.get("/{job_id}/history")
    async def get_history(
        job_id: str,
        limit: int = Query(20, le=100),
        offset: int = Query(0, ge=0),
        status: str | None = Query(None),
    ) -> Any:
        if job_id not in scheduler.registrations:
            return JSONResponse(
                status_code=404,
                content={"error": "Job not found", "code": "JOB_NOT_FOUND"},
            )
        items, total = await storage.get_executions(
            job_id, limit=limit, offset=offset, status=status
        )
        return {
            "items": [_execution_item(e) for e in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @router.get("/{job_id}/history/{exec_id}")
    async def get_execution(job_id: str, exec_id: str) -> Any:
        execution = await storage.get_execution(exec_id)
        if execution is None or execution.job_id != job_id:
            return JSONResponse(
                status_code=404,
                content={"error": "Execution not found", "code": "EXECUTION_NOT_FOUND"},
            )
        return {
            "id": execution.id,
            "jobId": execution.job_id,
            "jobName": execution.job_name,
            "status": execution.status,
            "startedAt": _serialize_dt(execution.started_at),
            "completedAt": _serialize_dt(execution.completed_at),
            "duration": execution.duration,
            "error": execution.error,
            "retryAttempt": execution.retry_attempt,
            "properties": execution.properties,
            "logs": [
                {
                    "timestamp": _serialize_dt(log.timestamp),
                    "level": log.level,
                    "message": log.message,
                }
                for log in execution.logs
            ],
        }

    @router.delete("/{job_id}/history/{exec_id}")
    async def delete_execution(job_id: str, exec_id: str) -> Any:
        """Deletes one finished run of the job and its logs."""
        try:
            await scheduler.delete_execution(job_id, exec_id)
            return {"message": "Run deleted"}
        except KeyError:
            return _job_not_found()
        except ExecutionNotFoundError:
            return JSONResponse(
                status_code=404,
                content={"error": "Execution not found", "code": "EXECUTION_NOT_FOUND"},
            )
        except InvalidOperationError as exc:
            return JSONResponse(
                status_code=409,
                content={"error": str(exc), "code": "EXECUTION_RUNNING"},
            )
        except NotSupportedError as exc:
            return _not_supported(exc)

    @router.delete("/{job_id}/history")
    async def delete_executions(job_id: str, status: str | None = Query(None)) -> Any:
        """Deletes every finished run of the job, or only those with ``status``."""
        try:
            deleted = await scheduler.delete_executions(job_id, status=status or None)
            return {"deleted": deleted}
        except KeyError:
            return _job_not_found()
        except NotSupportedError as exc:
            return _not_supported(exc)

    return router


def _job_not_found() -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error": "Job not found", "code": "JOB_NOT_FOUND"},
    )


def _not_supported(exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={"error": str(exc), "code": "NOT_SUPPORTED"},
    )
