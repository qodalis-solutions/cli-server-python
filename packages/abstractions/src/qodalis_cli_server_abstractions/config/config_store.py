"""Where writable settings are kept: the config stores."""

from __future__ import annotations

import abc
import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .config_types import CliConfigConflictError, CliConfigValue
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
    by one on every write.  Shipped: in memory (:class:`InMemoryConfigStore`),
    a JSON file (:class:`JsonFileConfigStore`) and a database
    (``CliDatabaseConfigStore`` of the databases plugin).  Secrets are stored
    as given: keep the file or table where only the server can read it."""

    #: The ``source`` of the settings it holds: ``runtime`` (memory), ``file`` or ``database``.
    source: str = "runtime"

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


class JsonFileConfigStore(ICliConfigStore):
    """Keeps the settings in a JSON file (source ``file``), read once and
    rewritten on every change (written to a temporary file, then renamed;
    mode 0600)::

        {"custom": {"custom": {"version": 2, "values": {"motd": "hello"}}}}

    Only this process writes the file: changes made to it while the server
    runs are overwritten."""

    source = "file"

    def __init__(self, file_path: str | os.PathLike[str]) -> None:
        self.file_path = os.fspath(file_path)
        self._data: Optional[_StoredData] = None

    def _load(self) -> _StoredData:
        if self._data is not None:
            return self._data
        parsed: Any = {}
        if os.path.exists(self.file_path):
            with open(self.file_path, encoding="utf-8") as f:
                text = f.read()
            try:
                parsed = json.loads(text) if text.strip() else {}
            except ValueError as exc:
                raise ValueError(f"config file {self.file_path} is not valid JSON: {exc}") from exc
        data: _StoredData = {}
        if isinstance(parsed, dict):
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
        self._data = data
        return data

    def _save(self, data: _StoredData) -> None:
        directory = os.path.dirname(os.path.abspath(self.file_path))
        os.makedirs(directory, exist_ok=True)
        temp = f"{self.file_path}.{os.getpid()}.tmp"
        body = {
            provider: {section: {"values": s.values, "version": s.version} for section, s in sections.items()}
            for provider, sections in data.items()
        }
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
            os.chmod(temp, 0o600)
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
