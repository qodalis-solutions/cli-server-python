"""Unit tests for ICliCommandProcessor.api_version."""

from __future__ import annotations

from qodalis_cli.abstractions import CliProcessCommand, ICliCommandProcessor

from .conftest import EchoProcessor, V2OnlyProcessor


class TestApiVersion:
    def test_default_api_version_is_1(self) -> None:
        proc = EchoProcessor()
        assert proc.api_version == 1

    def test_custom_processor_can_override_api_version(self) -> None:
        proc = V2OnlyProcessor()
        assert proc.api_version == 2
