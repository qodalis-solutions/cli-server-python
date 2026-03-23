"""Shared fixtures for the qodalis-cli test suite."""

from __future__ import annotations

import asyncio

import pytest

from qodalis_cli.abstractions import CliProcessCommand, ICliCommandProcessor
from qodalis_cli.services import CliCommandRegistry, CliCommandExecutorService
from qodalis_cli_server_abstractions import ICliStreamCommandProcessor


# ---------------------------------------------------------------------------
# Stub processors used across multiple test modules
# ---------------------------------------------------------------------------


class EchoProcessor(ICliCommandProcessor):
    """A trivial processor that echoes back the command value."""

    @property
    def command(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echoes input"

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return command.value or ""


class FailingProcessor(ICliCommandProcessor):
    """Processor that always raises."""

    @property
    def command(self) -> str:
        return "fail"

    @property
    def description(self) -> str:
        return "Always fails"

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        raise RuntimeError("boom")


class V2OnlyProcessor(ICliCommandProcessor):
    """Processor that targets API v2."""

    @property
    def command(self) -> str:
        return "v2cmd"

    @property
    def description(self) -> str:
        return "V2-only command"

    @property
    def api_version(self) -> int:
        return 2

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return "v2 result"


class ParentProcessor(ICliCommandProcessor):
    """Processor with nested sub-processors."""

    @property
    def command(self) -> str:
        return "parent"

    @property
    def description(self) -> str:
        return "Parent command"

    @property
    def processors(self) -> list[ICliCommandProcessor] | None:
        return [_ChildProcessor()]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return "parent result"


class _ChildProcessor(ICliCommandProcessor):
    @property
    def command(self) -> str:
        return "child"

    @property
    def description(self) -> str:
        return "Child command"

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return "child result"


class StreamProcessor(ICliCommandProcessor, ICliStreamCommandProcessor):
    """Processor that emits three streaming output chunks."""

    @property
    def command(self) -> str:
        return "stream-test"

    @property
    def description(self) -> str:
        return "Streaming test processor"

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return "non-streaming fallback"

    async def handle_stream_async(self, command, emit, cancellation_event: asyncio.Event | None = None) -> int:
        emit({"type": "text", "value": "chunk1"})
        emit({"type": "text", "value": "chunk2"})
        emit({"type": "text", "value": "chunk3"})
        return 0


class UnlistedParentProcessor(ICliCommandProcessor):
    """Parent that allows unlisted sub-commands."""

    @property
    def command(self) -> str:
        return "open"

    @property
    def description(self) -> str:
        return "Allows any subcommand"

    @property
    def allow_unlisted_commands(self) -> bool | None:
        return True

    @property
    def processors(self) -> list[ICliCommandProcessor] | None:
        return [_ChildProcessor()]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return "open result"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry() -> CliCommandRegistry:
    return CliCommandRegistry()


@pytest.fixture()
def populated_registry(registry: CliCommandRegistry) -> CliCommandRegistry:
    registry.register(EchoProcessor())
    registry.register(FailingProcessor())
    registry.register(V2OnlyProcessor())
    registry.register(ParentProcessor())
    registry.register(UnlistedParentProcessor())
    return registry


@pytest.fixture()
def executor(populated_registry: CliCommandRegistry) -> CliCommandExecutorService:
    return CliCommandExecutorService(populated_registry)
