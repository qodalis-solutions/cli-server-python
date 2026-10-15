"""MCP tools of the jobs plugin (``jobs_*``), registered on the CLI server's
MCP tool registry when the plugin is mounted.

The tools call the same :class:`CliJobScheduler` and job storage as the
``/api/v1/qcli/jobs`` routes.  Imported lazily (requires ``qodalis_cli``)."""

from __future__ import annotations

import json
from typing import Any

from qodalis_cli.mcp import McpCallContext, McpTool, McpToolResult, object_schema
from qodalis_cli.mcp.core import TIER_BUILTIN

from .cli_job_scheduler import CliJobScheduler, InvalidOperationError
from .cli_jobs_controller import _job_dto, _serialize_dt

JOBS_READ = "jobs:read"
JOBS_MANAGE = "jobs:manage"
READ_ONLY = {"readOnlyHint": True}
DESTRUCTIVE = {"destructiveHint": True}
JOB_NOT_FOUND = "Job not found"

_ID = {"type": "string", "description": "Job id (or name)"}


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


class JobsMcpToolProvider:
    """``jobs_list``, ``jobs_get``, ``jobs_history`` (``jobs:read``) and
    ``jobs_trigger/pause/resume/stop/cancel`` (``jobs:manage``)."""

    mcp_tier = TIER_BUILTIN
    name = "jobs"

    def __init__(self, scheduler: CliJobScheduler) -> None:
        self.scheduler = scheduler

    def _resolve(self, job_id: str) -> str | None:
        regs = self.scheduler.registrations
        if job_id in regs:
            return job_id
        matches = [r.id for r in regs.values() if r.options.name == job_id]
        return matches[0] if len(matches) == 1 else None

    def get_tools(self, context: McpCallContext) -> list[McpTool]:
        scheduler = self.scheduler

        async def jobs_list(args: dict[str, Any], _: McpCallContext) -> McpToolResult:
            jobs = [_job_dto(r) for r in scheduler.registrations.values()]
            lines = ["NAME | ID | STATUS | NEXT RUN | LAST STATUS"]
            lines += [
                f"{j['name']} | {j['id']} | {j['status']} | {j['nextRunAt'] or '-'} | {j['lastRunStatus'] or '-'}"
                for j in jobs
            ]
            return McpToolResult(text="\n".join(lines), structured={"jobs": jobs})

        async def jobs_get(args: dict[str, Any], _: McpCallContext) -> McpToolResult:
            job_id = self._resolve(args["id"])
            if job_id is None:
                return McpToolResult.error(JOB_NOT_FOUND)
            dto = _job_dto(scheduler.registrations[job_id])
            return McpToolResult(text=_json(dto), structured=dto)

        async def jobs_history(args: dict[str, Any], _: McpCallContext) -> McpToolResult:
            job_id = self._resolve(args["id"])
            if job_id is None:
                return McpToolResult.error(JOB_NOT_FOUND)
            limit = args.get("limit")
            limit = 20 if limit is None else int(limit)
            if limit < 1:
                return McpToolResult.error("limit must be at least 1")
            limit = min(limit, 100)
            items, total = await scheduler.storage.get_executions(job_id, limit=limit, offset=0)
            executions = [
                {
                    "id": e.id,
                    "status": e.status,
                    "startedAt": _serialize_dt(e.started_at),
                    "completedAt": _serialize_dt(e.completed_at),
                    "duration": e.duration,
                    "error": e.error,
                    "retryAttempt": e.retry_attempt,
                }
                for e in items
            ]
            lines = ["STARTED | STATUS | DURATION (ms) | ERROR"]
            lines += [
                f"{e['startedAt']} | {e['status']} | {e['duration'] if e['duration'] is not None else '-'}"
                f" | {e['error'] or ''}"
                for e in executions
            ]
            return McpToolResult(
                text="\n".join(lines),
                structured={"jobId": job_id, "items": executions, "total": total},
            )

        def action(method: str, done: str):
            async def call(args: dict[str, Any], _: McpCallContext) -> McpToolResult:
                job_id = self._resolve(args["id"])
                if job_id is None:
                    return McpToolResult.error(JOB_NOT_FOUND)
                try:
                    await getattr(scheduler, method)(job_id)
                except KeyError:
                    return McpToolResult.error(JOB_NOT_FOUND)
                except InvalidOperationError as exc:
                    return McpToolResult.error(str(exc))
                return McpToolResult(text=done)

            return call

        id_only = object_schema({"id": _ID}, ["id"])
        return [
            McpTool("jobs_list", "List background jobs", object_schema(), jobs_list, [JOBS_READ], dict(READ_ONLY)),
            McpTool("jobs_get", "Details of a background job", id_only, jobs_get, [JOBS_READ], dict(READ_ONLY)),
            McpTool(
                "jobs_history",
                "Recent executions of a background job",
                object_schema({
                    "id": _ID,
                    "limit": {"type": "integer", "default": 20, "description": "Maximum executions (1-100)"},
                }, ["id"]),
                jobs_history, [JOBS_READ], dict(READ_ONLY),
            ),
            McpTool("jobs_trigger", "Run a job now", id_only, action("trigger", "Job triggered"), [JOBS_MANAGE]),
            McpTool("jobs_pause", "Pause a job", id_only, action("pause", "Job paused"), [JOBS_MANAGE]),
            McpTool("jobs_resume", "Resume a paused job", id_only, action("resume", "Job resumed"), [JOBS_MANAGE]),
            McpTool("jobs_stop", "Stop a job (cancels a running execution)", id_only,
                    action("stop_job", "Job stopped"), [JOBS_MANAGE], dict(DESTRUCTIVE)),
            McpTool("jobs_cancel", "Cancel the running execution of a job", id_only,
                    action("cancel_current", "Execution cancelled"), [JOBS_MANAGE], dict(DESTRUCTIVE)),
        ]
