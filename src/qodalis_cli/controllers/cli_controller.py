from __future__ import annotations

import asyncio
import json
import logging
import platform
import time
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..abstractions import ICliCommandProcessor
from ..abstractions.cli_process_command import CliProcessCommand
from qodalis_cli_server_abstractions import is_stream_capable
from ..models import CliServerCommandDescriptor, CliServerCommandParameterDescriptorDto, CliServerResponse
from ..services.cli_command_executor_service import (
    ICliCommandExecutorService,
    caller_user_id,
    command_user_id,
    executing_as,
)
from ..services.cli_command_registry import ICliCommandRegistry
from ..services.cli_output_limits import DEFAULT_MAX_TABLE_ROWS, apply_output_limits
from ..security import get_auth_context, missing_processor_permissions

logger = logging.getLogger(__name__)

SERVER_VERSION = "1.0.0"


class ExecuteRequest(BaseModel):
    """Request body for the v1 command execution endpoint."""

    command: str = ""
    raw_command: str = Field(alias="rawCommand", default="")
    value: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    chain_commands: list[str] = Field(alias="chainCommands", default_factory=list)
    data: Any = None
    # Positional arguments split like a shell (quotes removed, flags excluded);
    # absent from older browsers, see ``get_positionals``.
    positionals: list[str] | None = None

    model_config = {"populate_by_name": True}


def _map_to_descriptor(processor: ICliCommandProcessor) -> dict[str, Any]:
    """Convert a command processor into a JSON-serialisable descriptor dict."""
    params = None
    if processor.parameters:
        params = [
            CliServerCommandParameterDescriptorDto(
                name=p.name,
                description=p.description,
                required=p.required,
                type=p.type,
                aliases=p.aliases,
                defaultValue=p.default_value,
            ).model_dump(by_alias=True, exclude_none=True)
            for p in processor.parameters
        ]

    subs = None
    if processor.processors:
        subs = [_map_to_descriptor(p) for p in processor.processors]

    desc = CliServerCommandDescriptor(
        command=processor.command,
        description=processor.description,
        version=processor.version,
        parameters=params if params else None,
        processors=subs if subs else None,
        allowUnlistedCommands=processor.allow_unlisted_commands,
        valueRequired=processor.value_required,
        author={"name": processor.author.name, "email": processor.author.email},
        requiredPermissions=list(getattr(processor, "required_permissions", None) or []) or None,
    )
    return desc.model_dump(by_alias=True, exclude_none=True)


def _chain_processors(root: ICliCommandProcessor | None, chain: list[str]) -> list[ICliCommandProcessor]:
    """The sub-processors along ``chain`` below ``root`` (each one's
    ``required_permissions`` applies to everything beneath it)."""
    out: list[ICliCommandProcessor] = []
    current = root
    for name in chain or []:
        if current is None or not isinstance(name, str):
            break
        current = next(
            (p for p in (current.processors or []) if p.command.lower() == name.lower()),
            None,
        )
        if current is not None:
            out.append(current)
    return out


def _permission_denied(registry: ICliCommandRegistry, request: Request, command: str, chain: list[str]) -> JSONResponse | None:
    """403 response when the caller lacks a processor's declared permissions."""
    ctx = get_auth_context(request)
    if ctx is None:
        return None
    root = registry.find_processor(command, None)
    resolved = registry.find_processor(command, list(chain) if chain else None)
    missing = missing_processor_permissions(ctx, root, *_chain_processors(root, chain), resolved)
    if missing:
        return JSONResponse(status_code=403, content={"error": f"missing permission: {', '.join(missing)}"})
    return None


def create_cli_router(
    registry: ICliCommandRegistry,
    executor: ICliCommandExecutorService,
    *,
    enable_shell: bool = False,
    shell_auth: Callable[[], str | None] | None = None,
    max_table_rows: int | None = None,
) -> APIRouter:
    """Create a FastAPI router for the v1 CLI API.

    Args:
        registry: The command registry to query for available processors.
        executor: The executor service used to run commands.
        max_table_rows: Row cap for unpaged tables emitted by stream
            processors (0 = no limit); defaults to the executor's.

    Returns:
        A configured ``APIRouter``.
    """
    router = APIRouter()
    if max_table_rows is None:
        max_table_rows = getattr(executor, "max_table_rows", DEFAULT_MAX_TABLE_ROWS)

    @router.get("/version")
    async def get_version() -> dict[str, str]:
        return {"version": SERVER_VERSION}

    @router.get("/capabilities")
    async def get_capabilities() -> dict[str, Any]:
        detected_os = (
            "darwin" if platform.system() == "Darwin"
            else "win32" if platform.system() == "Windows"
            else "linux"
        )

        shell = "powershell" if platform.system() == "Windows" else "bash"
        shell_path = (
            "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
            if platform.system() == "Windows"
            else "/bin/bash"
        )

        capabilities: dict[str, Any] = {
            "shell": enable_shell,
            "os": detected_os,
            "shellPath": shell_path,
            "version": SERVER_VERSION,
            "streaming": True,
        }
        if enable_shell:
            # users | token | custom | anonymous | locked (Appendix C1)
            capabilities["shellAuth"] = (shell_auth() if shell_auth else None) or "locked"
        return capabilities

    @router.get("/commands")
    async def get_commands() -> list[dict[str, Any]]:
        return [_map_to_descriptor(p) for p in registry.processors]

    @router.get("/commands/stats")
    async def get_command_stats() -> dict[str, Any]:
        """Executions per root command since the server started."""
        usage = getattr(executor, "usage", None)
        if usage is None:
            return {"since": None, "commands": []}
        return usage.snapshot()

    @router.post("/execute")
    async def execute_command(request: ExecuteRequest, http_request: Request) -> JSONResponse:
        denied = _permission_denied(registry, http_request, request.command, request.chain_commands)
        if denied is not None:
            return denied
        logger.debug("Executing command: %s", request.command)
        cmd = CliProcessCommand(
            command=request.command,
            raw_command=request.raw_command,
            value=request.value,
            args=request.args,
            chain_commands=request.chain_commands,
            data=request.data,
            positionals=request.positionals,
        )
        with executing_as(get_auth_context(http_request)):
            result = await executor.execute_async(cmd, cancellation_event=None)
        return JSONResponse(result.model_dump(by_alias=True, exclude_none=True))

    @router.post("/execute/stream")
    async def execute_stream(request: ExecuteRequest, http_request: Request):
        denied = _permission_denied(registry, http_request, request.command, request.chain_commands)
        if denied is not None:
            return denied
        command = CliProcessCommand(
            command=request.command,
            raw_command=request.raw_command,
            value=request.value,
            args=request.args,
            chain_commands=request.chain_commands,
            data=request.data,
            positionals=request.positionals,
        )

        cancellation_event = asyncio.Event()
        user_id = caller_user_id(get_auth_context(http_request))

        async def event_generator():
            # The generator runs in the response's own task: the executor
            # reads the caller from there.
            command_user_id.set(user_id)
            def sse_event(event_type: str, data: dict) -> str:
                return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

            started = time.perf_counter()
            # Counted here unless the executor runs (and counts) the command:
            # None = not counted here, else whether it failed.
            usage_failed: bool | None = True
            try:
                processor = registry.find_processor(
                    command.command,
                    list(command.chain_commands) if command.chain_commands else None,
                )

                if processor is None:
                    yield sse_event("error", {"message": f"Unknown command: {command.command}"})
                    return

                if executor.is_blocked(processor):
                    blocked_msg = f"Command '{command.command}' is currently disabled."
                    yield sse_event("error", {"message": blocked_msg})
                    return

                if is_stream_capable(processor):
                    # A client that goes away counts as an execution, not a failure.
                    usage_failed = False
                    queue: asyncio.Queue[str | None] = asyncio.Queue()

                    def emit_to_queue(output: dict) -> None:
                        if not cancellation_event.is_set():
                            queue.put_nowait(sse_event("output", apply_output_limits(output, max_table_rows)))

                    task = asyncio.ensure_future(
                        processor.handle_stream_async(command, emit_to_queue, cancellation_event)
                    )
                    while not task.done() or not queue.empty():
                        if cancellation_event.is_set():
                            task.cancel()
                            break
                        try:
                            chunk = queue.get_nowait()
                            yield chunk
                        except asyncio.QueueEmpty:
                            await asyncio.sleep(0)
                    if not cancellation_event.is_set():
                        try:
                            exit_code = task.result()
                        except Exception as exc:
                            usage_failed = True
                            yield sse_event("error", {"message": f"Error executing command: {exc}"})
                            return
                        usage_failed = exit_code not in (0, None)
                    else:
                        logger.debug("Stream cancelled for command: %s", command.command)
                        return
                else:
                    usage_failed = None
                    response = await executor.execute_async(command, cancellation_event)
                    for output in response.outputs:
                        yield sse_event("output", output.model_dump(by_alias=True, exclude_none=True))
                    exit_code = response.exit_code

                yield sse_event("done", {"exitCode": exit_code})
            except asyncio.CancelledError:
                logger.debug("Stream cancelled for command: %s", command.command)
            except Exception as exc:
                if usage_failed is not None:
                    usage_failed = True
                yield sse_event("error", {"message": f"Error executing command: {exc}"})
            finally:
                duration_ms = (time.perf_counter() - started) * 1000
                record = getattr(executor, "record_usage", None)
                if usage_failed is not None and callable(record):
                    record(command.command, duration_ms, usage_failed)
                notify = getattr(executor, "notify_executed", None)
                if usage_failed is not None and callable(notify):
                    await notify(command.command, duration_ms, usage_failed, user_id)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
