"""Registry for Data Explorer providers."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from qodalis_cli_server_abstractions import (
    DataExplorerProviderOptions,
    IDataExplorerProvider,
)


class _RegisteredProvider:
    """Internal wrapper that pairs a provider with its options."""

    __slots__ = ("provider", "options")

    def __init__(
        self,
        provider: IDataExplorerProvider,
        options: DataExplorerProviderOptions,
    ) -> None:
        self.provider = provider
        self.options = options


class DataExplorerRegistry:
    """Stores :class:`IDataExplorerProvider` instances keyed by source name."""

    def __init__(self) -> None:
        self._providers: dict[str, _RegisteredProvider] = {}

    def register(
        self,
        provider: IDataExplorerProvider,
        options: DataExplorerProviderOptions,
    ) -> None:
        """Register a provider under the source name defined in *options*."""
        self._providers[options.name] = _RegisteredProvider(provider, options)

    def get(self, source: str) -> tuple[IDataExplorerProvider, DataExplorerProviderOptions] | None:
        """Return the provider and its options for *source*, or ``None``."""
        entry = self._providers.get(source)
        if entry is None:
            return None
        return entry.provider, entry.options

    def get_sources(self) -> list[dict[str, Any]]:
        """Return a JSON-serialisable list of all registered sources."""
        return [
            asdict(entry.options) for entry in self._providers.values()
        ]
