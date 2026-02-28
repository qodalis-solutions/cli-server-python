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


class ExecuteRequest(BaseModel):
    command: str = ""
    raw_command: str = Field(alias="rawCommand", default="")
    value: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    chain_commands: list[str] = Field(alias="chainCommands", default_factory=list)
    data: Any = None

    model_config = {"populate_by_name": True}


def _map_to_descriptor(processor: ICliCommandProcessor) -> dict[str, Any]:
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
    )
    return desc.model_dump(by_alias=True, exclude_none=True)


def create_cli_router(
    registry: ICliCommandRegistry,
    executor: ICliCommandExecutorService,
) -> APIRouter:
    router = APIRouter()

    @router.get("/version")
    async def get_version() -> dict[str, str]:
        return {"version": "1.0.0"}

    @router.get("/commands")
    async def get_commands() -> list[dict[str, Any]]:
        return [_map_to_descriptor(p) for p in registry.processors]

    @router.post("/execute")
    async def execute_command(request: ExecuteRequest) -> JSONResponse:
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
