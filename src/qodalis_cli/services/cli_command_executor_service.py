from __future__ import annotations

import abc
import asyncio
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Sequence

from qodalis_cli_server_abstractions.events import CliEventAudience, ICliEventBus

from ..abstractions import CliProcessCommand, ICliCommandProcessor, ICliProcessorFilter
from ..models.cli_server_output import CliServerTextOutput
from ..models.cli_server_response import CliServerResponse
from .cli_command_registry import ICliCommandRegistry
from .cli_command_usage import CliCommandUsageTracker
from .cli_output_limits import DEFAULT_MAX_TABLE_ROWS, apply_response_limits

logger = logging.getLogger(__name__)

#: Permission of the ``command:executed`` event's audience.
AUDIT_READ = "audit:read"

#: The user running the current command (``command:executed``'s ``userId``),
#: set by the REST API and the MCP endpoint around an execution.
command_user_id: ContextVar[str | None] = ContextVar("qcli_command_user_id", default=None)


def caller_user_id(auth: Any) -> str | None:
    """The user id of an auth context (``None`` without one)."""
    user = getattr(auth, "user", None)
    user_id = getattr(user, "id", None)
    return user_id if isinstance(user_id, str) and user_id else None


@contextmanager
def executing_as(auth: Any) -> Iterator[None]:
    """Run commands in this block as the caller of ``auth`` (an auth context)."""
    token = command_user_id.set(caller_user_id(auth))
    try:
        yield
    finally:
        command_user_id.reset(token)


class ICliCommandExecutorService(abc.ABC):
    """Interface for a service that executes parsed CLI commands."""

    @abc.abstractmethod
    async def execute_async(
        self,
        command: CliProcessCommand,
        cancellation_event: asyncio.Event | None = None,
    ) -> CliServerResponse:
        """Execute a command and return a structured response.

        Args:
            command: The parsed command to execute.
            cancellation_event: Optional event that is set when the caller
                requests cancellation.

        Returns:
            A ``CliServerResponse`` containing outputs and an exit code.
        """
        ...

    @abc.abstractmethod
    def is_blocked(self, processor: "ICliCommandProcessor") -> bool:
        """Return True if any filter blocks this processor."""
        ...


class CliCommandExecutorService(ICliCommandExecutorService):
    """Default executor that resolves processors from a registry and runs them."""

    def __init__(
        self,
        registry: ICliCommandRegistry,
        filters: Sequence[ICliProcessorFilter] | None = None,
        max_table_rows: int = DEFAULT_MAX_TABLE_ROWS,
    ) -> None:
        """
        Args:
            registry: The command processor registry.
            filters: Optional processor filters for runtime enable/disable checks.
            max_table_rows: Unpaged tables longer than this are cut and marked
                truncated (``CliServerOptions.max_table_rows``; 0 = no limit).
        """
        self._registry = registry
        self._filters: list[ICliProcessorFilter] = list(filters or [])
        self.max_table_rows = max_table_rows
        #: Executions per root command since start (``GET /commands/stats``).
        self.usage = CliCommandUsageTracker()
        #: The server's event bus: ``command:executed`` after every command
        #: (set by ``create_cli_server``).
        self.event_bus: ICliEventBus | None = None

    def record_usage(self, command: str, duration_ms: float, failed: bool) -> None:
        """Count one execution of the root command ``command`` (named as the
        registry resolves it; unknown commands under their own name)."""
        name = (command or "").strip()
        if not name:
            return
        processor = self._registry.find_processor(name, None)
        self.usage.record(
            processor.command if processor is not None else name.split()[0],
            known=processor is not None,
            duration_ms=duration_ms,
            failed=failed,
        )

    async def notify_executed(
        self, command: str, duration_ms: float, failed: bool, user_id: str | None = None,
    ) -> None:
        """Publish ``command:executed`` for the root command ``command``: the
        processor's name only (never arguments or input), so unknown commands
        are not published.  ``user_id`` defaults to the current caller."""
        bus = self.event_bus
        name = (command or "").strip()
        if bus is None or not name:
            return
        processor = self._registry.find_processor(name, None)
        if processor is None:
            return
        data: dict[str, Any] = {
            "command": processor.command,
            "success": not failed,
            "durationMs": round(duration_ms),
        }
        user_id = user_id or command_user_id.get()
        if user_id:
            data["userId"] = user_id
        await bus.publish("command:executed", data, CliEventAudience(permission=AUDIT_READ))

    def add_filter(self, filter_: ICliProcessorFilter) -> None:
        """Register an additional processor filter at runtime.

        Args:
            filter_: The filter to add.
        """
        self._filters.append(filter_)

    def is_blocked(self, processor: ICliCommandProcessor) -> bool:
        """Return True if any filter blocks this processor."""
        return any(not f.is_allowed(processor) for f in self._filters)

    async def execute_async(
        self,
        command: CliProcessCommand,
        cancellation_event: asyncio.Event | None = None,
    ) -> CliServerResponse:
        started = time.perf_counter()
        failed = True
        try:
            response = await self._execute(command, cancellation_event)
            failed = response.exit_code != 0
            return response
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            self.record_usage(command.command, duration_ms, failed)
            await self.notify_executed(command.command, duration_ms, failed)

    async def _execute(
        self,
        command: CliProcessCommand,
        cancellation_event: asyncio.Event | None = None,
    ) -> CliServerResponse:
        chain = command.chain_commands if command.chain_commands else None
        full_command = (
            f"{command.command} {' '.join(chain)}" if chain else command.command
        )

        logger.info("Executing command: %s", full_command)

        processor = self._registry.find_processor(command.command, chain)

        if processor is None:
            logger.warning("Unknown command: %s", full_command)
            return CliServerResponse(
                exitCode=1,
                outputs=[
                    CliServerTextOutput(
                        value=f"Unknown command: {command.command}",
                        style="error",
                    )
                ],
            )

        if self.is_blocked(processor):
            logger.warning(
                "Command blocked by filter (plugin disabled): %s", full_command
            )
            return CliServerResponse(
                exitCode=1,
                outputs=[
                    CliServerTextOutput(
                        value=f"Command '{command.command}' is currently disabled.",
                        style="error",
                    )
                ],
            )

        try:
            structured = await processor.handle_structured_async(command, cancellation_event)
            if structured is not processor._STRUCTURED_NOT_IMPLEMENTED:
                logger.info("Command completed (structured): %s", full_command)
                return apply_response_limits(structured, self.max_table_rows)
            result = await processor.handle_async(command, cancellation_event)
            logger.info("Command completed: %s", full_command)
            return CliServerResponse(
                exitCode=0,
                outputs=[CliServerTextOutput(value=result)],
            )
        except Exception as exc:
            logger.error("Command failed: %s — %s", full_command, exc, exc_info=True)
            return CliServerResponse(
                exitCode=1,
                outputs=[
                    CliServerTextOutput(
                        value=f"Error executing command: {exc}",
                        style="error",
                    )
                ],
            )
