"""Shared fixtures for the qodalis-cli test suite."""

from __future__ import annotations

import asyncio
import faulthandler
import sys
import threading
import traceback

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


class SlowProcessor(ICliCommandProcessor):
    """Processor that runs until cancelled, recording whether cancellation was observed."""

    # Set by the test to inspect processor behaviour
    cancellation_observed: bool = False

    @property
    def command(self) -> str:
        return "slow"

    @property
    def description(self) -> str:
        return "Runs forever until cancelled"

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        SlowProcessor.cancellation_observed = False
        # Poll the cancellation event so the processor can exit early
        for _ in range(1000):
            if cancellation_event is not None and cancellation_event.is_set():
                SlowProcessor.cancellation_observed = True
                return "cancelled"
            await asyncio.sleep(0.01)
        return "finished"


class SlowStreamProcessor(ICliCommandProcessor, ICliStreamCommandProcessor):
    """Stream processor that respects cancellation between chunks."""

    cancellation_observed: bool = False

    @property
    def command(self) -> str:
        return "slow-stream"

    @property
    def description(self) -> str:
        return "Streams chunks until cancelled"

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return "non-streaming fallback"

    async def handle_stream_async(self, command, emit, cancellation_event: asyncio.Event | None = None) -> int:
        SlowStreamProcessor.cancellation_observed = False
        for i in range(1000):
            if cancellation_event is not None and cancellation_event.is_set():
                SlowStreamProcessor.cancellation_observed = True
                return 1
            emit({"type": "text", "value": f"chunk{i}"})
            await asyncio.sleep(0.01)
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
    registry.register(ParentProcessor())
    registry.register(UnlistedParentProcessor())
    return registry


@pytest.fixture()
def executor(populated_registry: CliCommandRegistry) -> CliCommandExecutorService:
    return CliCommandExecutorService(populated_registry)


# ---------------------------------------------------------------------------
# Exit diagnostics: the suite once hung after its summary (CI would wait for
# the job's timeout).  After the session, list the non-daemon threads still
# running (they keep the interpreter alive) with their stacks, and if the
# process has not exited a minute later, dump every thread's stack (repeated)
# so the log shows what holds it.  Diagnostics only: nothing is killed.
# ---------------------------------------------------------------------------


def _idle_executor_worker(frame) -> bool:
    """A ``ThreadPoolExecutor`` worker waiting for work: it exits at interpreter shutdown."""
    stack = traceback.extract_stack(frame)
    return any(f.name == "_worker" and f.filename.endswith("concurrent/futures/thread.py") for f in stack[-3:])


def pytest_unconfigure(config: pytest.Config) -> None:
    frames = sys._current_frames()
    busy = []
    for thread in threading.enumerate():
        if thread is threading.main_thread() or thread.daemon or not thread.is_alive():
            continue
        frame = frames.get(thread.ident)
        if frame is not None and _idle_executor_worker(frame):
            continue
        busy.append((thread, frame))
    if busy:
        out = sys.__stderr__
        out.write(f"\n[qcli tests] {len(busy)} non-daemon thread(s) still running after the session:\n")
        for thread, frame in busy:
            out.write(f"--- {thread.name} (ident {thread.ident})\n")
            if frame is not None:
                out.write("".join(traceback.format_stack(frame)))
        out.flush()
    # Still alive a minute from now: every thread's stack, every minute, until the process exits
    faulthandler.dump_traceback_later(60, repeat=True, file=sys.__stderr__)
