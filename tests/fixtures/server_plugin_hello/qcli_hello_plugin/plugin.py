"""The plugin's entry point (``qodalis_cli.plugins``): settings, a command and a route; the abstractions are the
server's own (never installed with the plugin)."""

from __future__ import annotations

from typing import Any

import qodalis_cli_server_abstractions as abstractions
from fastapi import APIRouter
from qodalis_cli_server_abstractions import CliCommandProcessor, CliModule
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition, CliConfigValidation

from .greeting import greet

#: The abstractions this plugin imported (the tests check they are the server's).
ABSTRACTIONS_FILE = abstractions.__file__
last_context: Any = None


class _Plugin:
    def __init__(self, prefix: str, router: APIRouter, module: Any) -> None:
        self.prefix = prefix
        self.router = router
        self.module = module


async def create_plugin(context: Any) -> Any:
    global last_context
    last_context = context
    settings = await context.settings(title="Hello", settings=[
        CliConfigSettingDefinition(key="greeting", label="Greeting", type="string", default="Hello"),
        CliConfigSettingDefinition(key="times", type="number", default=1, validation=CliConfigValidation(min=1, max=5)),
    ])

    def value(key: str) -> Any:
        return settings.value("settings", key)

    class HelloCommand(CliCommandProcessor):
        command = "hello"
        description = "Says hello"

        async def handle_async(self, command: Any, cancellation_event: Any = None) -> str:
            return greet(value("greeting"), command.value or "world", value("times"))

    class HelloModule(CliModule):
        name = "hello"
        version = "1.2.0"
        description = "Says hello"
        processors = [HelloCommand()]

    router = APIRouter()

    @router.get("")
    async def index() -> dict[str, Any]:
        return {"greeting": value("greeting"), "dataDir": context.data_dir}

    return _Plugin("/api/v1/qcli/hello", router, HelloModule())
