from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..abstractions import ICliCommandProcessor
from ..abstractions.cli_process_command import CliProcessCommand
from ..models import CliServerCommandDescriptor, CliServerCommandParameterDescriptorDto, CliServerResponse
from ..services.cli_command_executor_service import ICliCommandExecutorService
from ..services.cli_command_registry import ICliCommandRegistry


class ExecuteRequestV2(BaseModel):
    """Request body for the v2 command execution endpoint."""

    command: str = ""
    raw_command: str = Field(alias="rawCommand", default="")
    value: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    chain_commands: list[str] = Field(alias="chainCommands", default_factory=list)
    data: Any = None

    model_config = {"populate_by_name": True}


def _map_to_descriptor_v2(processor: ICliCommandProcessor) -> dict[str, Any]:
    """Convert a command processor into a v2 JSON-serialisable descriptor dict."""
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
        subs = [_map_to_descriptor_v2(p) for p in processor.processors]

    desc = CliServerCommandDescriptor(
        command=processor.command,
        description=processor.description,
        version=processor.version,
        apiVersion=processor.api_version,
        parameters=params if params else None,
        processors=subs if subs else None,
        allowUnlistedCommands=processor.allow_unlisted_commands,
        valueRequired=processor.value_required,
        author={"name": processor.author.name, "email": processor.author.email},
    )
    return desc.model_dump(by_alias=True, exclude_none=True)


def create_cli_router_v2(
    registry: ICliCommandRegistry,
    executor: ICliCommandExecutorService,
) -> APIRouter:
    """Create a FastAPI router for the v2 CLI API.

    Args:
        registry: The command registry to query for available processors.
        executor: The executor service used to run commands.

    Returns:
        A configured ``APIRouter`` serving only processors with ``api_version >= 2``.
    """
    router = APIRouter()

    @router.get("/version")
    async def get_version_v2() -> dict[str, Any]:
        return {"apiVersion": 2, "serverVersion": "2.0.0"}

    @router.get("/commands")
    async def get_commands_v2() -> list[dict[str, Any]]:
        return [
            _map_to_descriptor_v2(p)
            for p in registry.processors
            if p.api_version >= 2
        ]

    @router.post("/execute")
    async def execute_command_v2(request: ExecuteRequestV2) -> JSONResponse:
        cmd = CliProcessCommand(
            command=request.command,
            raw_command=request.raw_command,
            value=request.value,
            args=request.args,
            chain_commands=request.chain_commands,
            data=request.data,
        )
        result = await executor.execute_async(cmd)
        return JSONResponse(result.model_dump(by_alias=True, exclude_none=True))

    return router
