"""Unit tests for CliCommandExecutorService."""

from __future__ import annotations

import pytest

from qodalis_cli.abstractions import CliProcessCommand
from qodalis_cli.services import CliCommandExecutorService


class TestExecuteAsync:
    @pytest.mark.asyncio
    async def test_known_command_returns_success(
        self, executor: CliCommandExecutorService
    ) -> None:
        cmd = CliProcessCommand(command="echo", value="hello")
        result = await executor.execute_async(cmd)
        assert result.exit_code == 0
        assert len(result.outputs) == 1
        assert result.outputs[0].value == "hello"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_unknown_command_returns_error(
        self, executor: CliCommandExecutorService
    ) -> None:
        cmd = CliProcessCommand(command="nonexistent")
        result = await executor.execute_async(cmd)
        assert result.exit_code == 1
        assert len(result.outputs) == 1
        assert "Unknown command" in result.outputs[0].value  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_throwing_command_returns_error(
        self, executor: CliCommandExecutorService
    ) -> None:
        cmd = CliProcessCommand(command="fail")
        result = await executor.execute_async(cmd)
        assert result.exit_code == 1
        assert len(result.outputs) == 1
        assert "boom" in result.outputs[0].value  # type: ignore[union-attr]
