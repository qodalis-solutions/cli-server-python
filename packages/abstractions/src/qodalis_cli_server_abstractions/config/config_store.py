"""Where writable settings are kept: the config stores."""

from __future__ import annotations

import abc
import copy
import inspect
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .config_types import (
    CliConfigConflictError,
    CliConfigProviderStorage,
    CliConfigStoreDescription,
    CliConfigValue,
)
from .config_validation import clone_cli_config_value


@dataclass
class CliConfigStoredSection:
    """A section's stored values (the settings changed from their defaults)
    and its version (0 when nothing is stored)."""

    values: dict[str, CliConfigValue] = field(default_factory=dict)
    version: int = 0


class ICliConfigStore(abc.ABC):
    """Where writable settings are kept: the values changed from their
    defaults, per provider and section, with a version per section that grows
    by one on every write.  Every provider may have its own store, and one
    store may serve several providers (its data is keyed by provider and
    section).  Shipped: in memory (:class:`InMemoryConfigStore`), a JSON file
    or a section of the app's JSON settings file (:class:`JsonFileConfigStore`),
    a database (``CliDatabaseConfigStore`` of the databases plugin) and Redis
    (``CliRedisConfigStore``, ``qodalis-cli-server-plugin-config-redis``).
    Secrets are stored as given: keep the file, table or keys where only the
    server can read them."""

    #: The ``source`` of the settings it holds: ``runtime`` (memory), ``file`` or ``database`` (Redis too).
    source: str = "runtime"

    #: Optional: resolves once the store can be used (e.g. its table exists);
    #: ``server.config.ready()`` awaits it, besides every provider's stored
    #: values.  A method returning an awaitable, or an awaitable that can be
    #: awaited more than once (a ``Future`` or ``Task``); ``None`` when not needed.
    ready: Any = None

    # Stores may define ``describe() -> CliConfigStoreDescription``: where they
    # keep the settings, shown by ``GET /config`` (each stored provider's
    # ``store``, and ``defaultStore``), the dashboard's Config page and
    # ``config list``: the file name, the database and table or the Redis
    # host, never credentials.  A store without it is shown as a ``custom``
    # store (:func:`describe_cli_config_store`).

    @abc.abstractmethod
    async def read(self, provider: str, section: str) -> CliConfigStoredSection:
        """A section's values; ``CliConfigStoredSection({}, 0)`` when nothing is stored."""

    @abc.abstractmethod
    async def write(
        self,
        provider: str,
        section: str,
        changes: Mapping[str, Optional[CliConfigValue]],
        expected_version: Optional[int] = None,
    ) -> CliConfigStoredSection:
        """Applies ``changes`` to a section atomically (``None`` removes a key)
        and returns the section as stored.  With ``expected_version``, raises
        :class:`CliConfigConflictError` when the stored version differs."""


def cli_config_store_storage() -> CliConfigProviderStorage:
    """The ``storage`` of a provider whose changes are kept in a config store (the provider view's ``store`` says which)."""
    return CliConfigProviderStorage(kind="store", description="Changes are saved to its config store")


#: The ``storage`` of a provider whose changes are kept in a config store.
CLI_CONFIG_STORE_STORAGE = cli_config_store_storage()


def describe_cli_config_store(store: Any) -> CliConfigStoreDescription:
    """A store's description: its own ``describe()``, else a ``custom`` store named after its class."""
    describe = getattr(store, "describe", None)
    if callable(describe):
        d = describe()
        if isinstance(d, Mapping):
            d = CliConfigStoreDescription(
                kind=d.get("kind", "custom"), title=d.get("title", ""),
                detail=d.get("detail") or "", persistent=d.get("persistent") is True,
            )
        return CliConfigStoreDescription(
            kind=d.kind, title=d.title, detail=d.detail or "", persistent=d.persistent is True,
        )
    name = type(store).__name__
    return CliConfigStoreDescription(
        kind="custom",
        title="Custom store",
        detail=name if name and name != "object" else "",
        persistent=getattr(store, "source", "runtime") != "runtime",
    )


def describe_cli_config_store_line(store: CliConfigStoreDescription) -> str:
    """The store as one line: ``JSON file (qcli-demo-config.json), kept across restarts``."""
    kept = "kept across restarts" if store.persistent else "changes are lost on restart"
    return f"{describe_cli_config_store_name(store)}, {kept}"


def describe_cli_config_store_name(store: CliConfigStoreDescription) -> str:
    """The store's title and detail: ``Redis (cache:6379/0, prefix qcli:config)``, ``In memory``."""
    detail = f" ({store.detail})" if store.detail else ""
    return f"{store.title}{detail}"


async def cli_config_store_ready(store: Any) -> None:
    """Awaits a store's own ``ready`` (a method returning an awaitable, or an awaitable), when it has one."""
    ready = getattr(store, "ready", None)
    if ready is None:
        return
    if callable(ready):
        ready = ready()
    if inspect.isawaitable(ready):
        await ready


def _display_path(file_path: str) -> str:
    """A path relative to the working directory when inside it, else the file name only (no absolute paths in descriptions)."""
    absolute = os.path.abspath(file_path)
    try:
        relative = os.path.relpath(absolute, os.getcwd())
    except ValueError:
        relative = ""
    if relative and relative != "." and not relative.startswith("..") and not os.path.isabs(relative):
        return relative.replace(os.sep, "/")
    return os.path.basename(absolute)


def apply_cli_config_changes(
    values: Mapping[str, CliConfigValue], changes: Mapping[str, Optional[CliConfigValue]],
) -> dict[str, CliConfigValue]:
    """Applies changes to stored values: ``None`` removes the key."""
    result = dict(values)
    for key, value in changes.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = clone_cli_config_value(value)
    return result


_StoredData = dict[str, dict[str, CliConfigStoredSection]]


def _write_section(
    data: _StoredData,
    provider: str,
    section: str,
    changes: Mapping[str, Optional[CliConfigValue]],
    expected_version: Optional[int],
) -> CliConfigStoredSection:
    current = data.get(provider, {}).get(section) or CliConfigStoredSection()
    if expected_version is not None and expected_version != current.version:
        raise CliConfigConflictError(str(current.version))
    stored = CliConfigStoredSection(apply_cli_config_changes(current.values, changes), current.version + 1)
    data.setdefault(provider, {})[section] = stored
    return CliConfigStoredSection(copy.deepcopy(stored.values), stored.version)


def _read_section(data: _StoredData, provider: str, section: str) -> CliConfigStoredSection:
    current = data.get(provider, {}).get(section)
    if current is None:
        return CliConfigStoredSection()
    return CliConfigStoredSection(copy.deepcopy(current.values), current.version)


class InMemoryConfigStore(ICliConfigStore):
    """Keeps the settings in memory: changes apply at runtime and are lost on restart (source ``runtime``)."""

    source = "runtime"

    def __init__(self) -> None:
        self._data: _StoredData = {}

    def describe(self) -> CliConfigStoreDescription:
        return CliConfigStoreDescription(kind="memory", title="In memory", detail="", persistent=False)

    async def read(self, provider: str, section: str) -> CliConfigStoredSection:
        return _read_section(self._data, provider, section)

    async def write(
        self,
        provider: str,
        section: str,
        changes: Mapping[str, Optional[CliConfigValue]],
        expected_version: Optional[int] = None,
    ) -> CliConfigStoredSection:
        return _write_section(self._data, provider, section, changes, expected_version)


def _parse_stored_data(parsed: Any) -> _StoredData:
    """Stored data from parsed JSON: malformed sections and versions are skipped."""
    data: _StoredData = {}
    if not isinstance(parsed, dict):
        return data
    for provider, sections in parsed.items():
        if not isinstance(sections, dict):
            continue
        for section, stored in sections.items():
            if not isinstance(stored, dict):
                continue
            values = stored.get("values")
            version = stored.get("version")
            data.setdefault(provider, {})[section] = CliConfigStoredSection(
                values if isinstance(values, dict) else {},
                version if isinstance(version, int) and not isinstance(version, bool) and version >= 0 else 0,
            )
    return data


_INDENT = re.compile(r"^([ \t]+)\S", re.MULTILINE)


class JsonFileConfigStore(ICliConfigStore):
    """Keeps the settings in a JSON file (source ``file``), read once and
    rewritten on every change (written to a temporary file, then renamed)::

        {"custom": {"custom": {"version": 2, "values": {"motd": "hello"}}}}

    The whole file by default (created with mode 0600; only this process
    writes it: changes made to it while the server runs are overwritten).
    With ``section``, one section of the app's own settings file:
    ``JsonFileConfigStore("config/app.json", section="qcli")`` keeps the
    settings under ``"qcli"`` (a nested one as ``app.qcli`` or ``app:qcli``),
    re-reads the file on every write and leaves everything else in it (keys,
    order, indentation, the file's mode) as it was; the section is created
    when missing."""

    source = "file"

    def __init__(self, file_path: str | os.PathLike[str], section: Optional[str] = None) -> None:
        self.file_path = os.fspath(file_path)
        text = (section or "").strip()
        #: The section's path in the file (``("app", "qcli")``); empty for the whole file.
        self.section_path: tuple[str, ...] = tuple(re.split(r"[.:]", text)) if text else ()
        if any(not part for part in self.section_path):
            raise ValueError(f"invalid config file section: {section}")
        self._data: Optional[_StoredData] = None

    def describe(self) -> CliConfigStoreDescription:
        """The file's path relative to the working directory, or its name when
        outside it; and the section (``config/app.json, section qcli``)."""
        section = f", section {'.'.join(self.section_path)}" if self.section_path else ""
        return CliConfigStoreDescription(
            kind="file", title="JSON file", detail=f"{_display_path(self.file_path)}{section}", persistent=True,
        )

    def _read_file(self) -> tuple[Any, str]:
        """The whole file parsed (``{}`` when missing or empty) and its text."""
        if not os.path.exists(self.file_path):
            return {}, ""
        with open(self.file_path, encoding="utf-8") as f:
            text = f.read()
        try:
            return (json.loads(text) if text.strip() else {}), text
        except ValueError as exc:
            raise ValueError(f"config file {self.file_path} is not valid JSON: {exc}") from exc

    def _section_of(self, root: Any) -> Any:
        """The section's value in the parsed file (``None`` when missing);
        raises when a part of the path is not an object."""
        node = root
        for i, part in enumerate(self.section_path):
            if not isinstance(node, dict):
                where = ".".join(self.section_path[:i]) or "the file"
                raise ValueError(f"config file {self.file_path}: {where} is not an object")
            if part not in node:
                return None
            node = node[part]
        if not isinstance(node, dict):
            raise ValueError(f"config file {self.file_path}: {'.'.join(self.section_path)} is not an object")
        return node

    def _load(self) -> _StoredData:
        if self._data is not None:
            return self._data
        root, _ = self._read_file()
        self._data = _parse_stored_data(self._section_of(root) if self.section_path else root)
        return self._data

    def _save(self, data: _StoredData) -> None:
        target = os.path.abspath(self.file_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        body = {
            provider: {section: {"values": s.values, "version": s.version} for section, s in sections.items()}
            for provider, sections in data.items()
        }
        mode = 0o600
        if self.section_path:
            # The rest of the file as it is now: re-read it, replace the section only
            root, current = self._read_file()
            if not isinstance(root, dict):
                root = {}
            self._section_of(root)
            node = root
            for part in self.section_path[:-1]:
                node = node.setdefault(part, {})
            node[self.section_path[-1]] = body
            match = _INDENT.search(current)
            indent = match.group(1) if match else "  "
            newline = "" if current and not current.endswith("\n") else "\n"
            text = json.dumps(root, indent=indent, ensure_ascii=False) + newline
            if os.path.exists(target):
                mode = os.stat(target).st_mode & 0o777
        else:
            text = json.dumps(body, indent=2, ensure_ascii=False) + "\n"
        temp = f"{self.file_path}.{os.getpid()}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(temp, mode)
            os.replace(temp, self.file_path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    async def read(self, provider: str, section: str) -> CliConfigStoredSection:
        return _read_section(self._load(), provider, section)

    async def write(
        self,
        provider: str,
        section: str,
        changes: Mapping[str, Optional[CliConfigValue]],
        expected_version: Optional[int] = None,
    ) -> CliConfigStoredSection:
        # No await in between: one write at a time within the event loop
        data = self._load()
        snapshot = copy.deepcopy(data)
        result = _write_section(data, provider, section, changes, expected_version)
        try:
            self._save(data)
        except BaseException:
            # Not written: keep what is on disk
            self._data = snapshot
            raise
        return result
