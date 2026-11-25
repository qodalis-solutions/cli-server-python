"""The config providers of a server and the stores of their writable settings."""

from __future__ import annotations

import logging
from typing import Any, Optional

from qodalis_cli_server_abstractions.config import (
    CLI_CONFIG_PROVIDER_ID,
    ICliConfigStore,
    InMemoryConfigStore,
    cli_config_store_ready,
)

logger = logging.getLogger("qodalis_cli.config")

#: Default ``order`` of a provider that sets none.
DEFAULT_CONFIG_PROVIDER_ORDER = 100


def _order(provider: Any) -> int:
    order = getattr(provider, "order", None)
    return order if isinstance(order, int) and not isinstance(order, bool) else DEFAULT_CONFIG_PROVIDER_ORDER


def _is_store_attachable(provider: Any) -> bool:
    """A provider that keeps its settings in a config store (e.g. a ``CliStoredConfigProvider``), maybe its own."""
    return callable(getattr(provider, "attach_store", None))


def _is_config_store(store: Any) -> bool:
    return store is not None and callable(getattr(store, "read", None)) and callable(getattr(store, "write", None))


class CliConfigRegistry:
    """The config providers of a server (``builder.config_providers``) and the
    stores their writable settings are kept in.  Every provider that keeps
    settings in a store (``attach_store``, e.g. a ``CliStoredConfigProvider``)
    gets one, resolved in this order: the host's store for its id
    (``builder.set_config_store(store, id)``, ``config stores={...}``), the
    provider's own (``own_store``), the default store
    (``builder.set_config_store(store)``, ``config store=...``), else in
    memory.  A store may serve several providers.  Started by
    ``create_cli_server``: from then on every provider gets its store (those
    registered later when added) and the stored values are applied, at the
    app's startup or on the first config request, whichever comes first."""

    def __init__(self) -> None:
        self._providers: list[Any] = []
        self._default_store: Optional[ICliConfigStore] = None
        self._memory_store: ICliConfigStore = InMemoryConfigStore()
        self._stores: dict[str, ICliConfigStore] = {}
        self._attached: dict[str, ICliConfigStore] = {}
        self._started = False
        self._pending: list[Any] = []
        self._error: Optional[BaseException] = None

    def add(self, provider: Any) -> None:
        """Registers a provider.  Raises ``ValueError``: ``invalid config
        provider: <id>`` or ``config provider already registered: <id>``."""
        provider_id = getattr(provider, "id", None)
        if (
            not isinstance(provider_id, str)
            or not CLI_CONFIG_PROVIDER_ID.match(provider_id)
            or not callable(getattr(provider, "get_sections", None))
        ):
            raise ValueError(f"invalid config provider: {provider_id}")
        title = getattr(provider, "title", None)
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"invalid config provider: {provider_id} needs a title")
        if self.has(provider_id):
            raise ValueError(f"config provider already registered: {provider_id}")
        self._providers.append(provider)
        if self._started and _is_store_attachable(provider):
            self._resolve_now(provider)

    def has(self, provider_id: str) -> bool:
        """True when a provider with this id is registered."""
        return any(p.id == provider_id for p in self._providers)

    def find(self, provider_id: str) -> Any:
        return next((p for p in self._providers if p.id == provider_id), None)

    @property
    def providers(self) -> list[Any]:
        """The providers sorted by ``order`` (default 100), then id."""
        return sorted(self._providers, key=lambda p: (_order(p), p.id))

    @property
    def default_store(self) -> ICliConfigStore:
        """The store providers use unless the host set one for their id or they have their own (in memory unless set)."""
        return self._default_store if self._default_store is not None else self._memory_store

    @property
    def provider_stores(self) -> dict[str, ICliConfigStore]:
        """The stores the host set per provider id (``set_store(store, id)``)."""
        return dict(self._stores)

    def set_store(self, store: ICliConfigStore, provider_id: Optional[str] = None) -> None:
        """Keeps writable settings in ``store``: of every provider without a
        store of its own (the default store), or, with ``provider_id``, of
        that provider (whatever its own store).  The default store before the
        server starts the registry; a provider's before it is registered."""
        if not _is_config_store(store):
            raise ValueError("invalid config store: it needs read and write")
        if provider_id is None:
            if self._started:
                raise RuntimeError("the config store must be set before the server is created")
            self._default_store = store
            return
        if not isinstance(provider_id, str) or not CLI_CONFIG_PROVIDER_ID.match(provider_id):
            raise ValueError(f"invalid config provider id: {provider_id}")
        if provider_id in self._attached:
            raise RuntimeError(f"the config store of {provider_id} must be set before the provider is registered")
        self._stores[provider_id] = store

    def store_of(self, provider: Any) -> Optional[ICliConfigStore]:
        """The store a provider's settings are kept in: the one it was given
        when registered, else the one it would get now; ``None`` for a
        provider that keeps none (read-only, or saving itself)."""
        p = self.find(provider) if isinstance(provider, str) else provider
        if p is None or not _is_store_attachable(p):
            return None
        attached = self._attached.get(p.id)
        return attached if attached is not None else self._resolve(p)

    def _resolve(self, provider: Any) -> ICliConfigStore:
        """The resolution order: the host's store for the id, the provider's own, the default store, memory."""
        host = self._stores.get(provider.id)
        if host is not None:
            return host
        own = getattr(provider, "own_store", None)
        return own if _is_config_store(own) else self.default_store

    def _resolve_now(self, provider: Any) -> None:
        """Gives a provider its store (attached by :meth:`ready`); from then on its store is fixed."""
        self._attached[provider.id] = self._resolve(provider)
        self._pending.append(provider)

    @property
    def stores(self) -> list[ICliConfigStore]:
        """Every store in use (each once), the default first."""
        result: list[ICliConfigStore] = [self.default_store]
        for provider in self._providers:
            store = self.store_of(provider)
            if store is not None and not any(s is store for s in result):
                result.append(store)
        return result

    def start(self) -> None:
        """Gives the providers their store (once, attached by :meth:`ready`);
        providers added later get theirs when added."""
        if self._started:
            return
        self._started = True
        for provider in self._providers:
            if _is_store_attachable(provider):
                self._resolve_now(provider)

    @property
    def started(self) -> bool:
        return self._started

    async def ready(self) -> None:
        """Waits for every store's own ``ready`` and attaches their store to the
        providers not attached yet, which load and apply their stored
        settings.  Raises the first failure (again on every call: a config
        store that cannot be read fails every config request)."""
        for store in self.stores:
            try:
                await cli_config_store_ready(store)
            except Exception as exc:
                logger.error("A config store is not ready: %s", exc)
                if self._error is None:
                    self._error = exc
        while self._pending:
            provider = self._pending.pop(0)
            try:
                await provider.attach_store(self._attached[provider.id])
            except Exception as exc:
                logger.error("Loading the settings of config provider '%s' failed: %s", provider.id, exc)
                if self._error is None:
                    self._error = exc
        if self._error is not None:
            raise self._error
