"""FastAPI router for the background jobs API."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from qodalis_cli_server_abstractions.jobs import ICliJobStorageProvider

from .cli_job_scheduler import CliJobScheduler, InvalidOperationError
from .interval_parser import parse_interval


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


def create_cli_jobs_router(
    scheduler: CliJobScheduler,
    storage: ICliJobStorageProvider,
) -> APIRouter:
    """Create a FastAPI router with CRUD and control endpoints for jobs."""
    router = APIRouter()

    @router.get("")
    async def list_jobs() -> list[dict[str, Any]]:
        return [_job_dto(reg) for reg in scheduler.registrations.values()]

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
            "items": [
                {
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
                }
                for e in items
            ],
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
            "logs": [
                {
                    "timestamp": _serialize_dt(log.timestamp),
                    "level": log.level,
                    "message": log.message,
                }
                for log in execution.logs
            ],
        }

    return router
