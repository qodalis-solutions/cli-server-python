"""Unit tests for CliCommandRegistry."""

from __future__ import annotations

from qodalis_cli.services import CliCommandRegistry

from .conftest import (
    EchoProcessor,
    ParentProcessor,
    UnlistedParentProcessor,
)


class TestRegisterAndFind:
    def test_register_and_find_by_command(self, registry: CliCommandRegistry) -> None:
        proc = EchoProcessor()
        registry.register(proc)
        assert registry.find_processor("echo") is proc

    def test_find_processor_is_case_insensitive(self, registry: CliCommandRegistry) -> None:
        proc = EchoProcessor()
        registry.register(proc)
        assert registry.find_processor("ECHO") is proc
        assert registry.find_processor("Echo") is proc
        assert registry.find_processor("eCHo") is proc

    def test_find_processor_returns_none_for_unknown(self, registry: CliCommandRegistry) -> None:
        assert registry.find_processor("nonexistent") is None

    def test_processors_property_lists_all(self, registry: CliCommandRegistry) -> None:
        p1 = EchoProcessor()
        registry.register(p1)
        assert list(registry.processors) == [p1]


class TestChainResolution:
    def test_chain_resolves_child(self, registry: CliCommandRegistry) -> None:
        registry.register(ParentProcessor())
        result = registry.find_processor("parent", ["child"])
        assert result is not None
        assert result.command == "child"

    def test_chain_unknown_subcommand_returns_none(self, registry: CliCommandRegistry) -> None:
        registry.register(ParentProcessor())
        result = registry.find_processor("parent", ["nonexistent"])
        assert result is None

    def test_chain_unknown_subcommand_with_allow_unlisted(self, registry: CliCommandRegistry) -> None:
        registry.register(UnlistedParentProcessor())
        result = registry.find_processor("open", ["anything"])
        assert result is not None
        # Should return the parent itself when subcommand is unknown but unlisted allowed
        assert result.command == "open"

    def test_chain_known_subcommand_with_allow_unlisted(self, registry: CliCommandRegistry) -> None:
        registry.register(UnlistedParentProcessor())
        result = registry.find_processor("open", ["child"])
        assert result is not None
        assert result.command == "child"
