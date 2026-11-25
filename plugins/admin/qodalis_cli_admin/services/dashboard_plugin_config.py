"""Settings of the dashboard plugins installed from the marketplace: a plugin's ``config`` as the marketplace publishes
it (``{ entries: [...] }``, checked as strictly as the marketplace does), values given for it, and the
``dashboard-plugins`` config provider with one section per installed plugin with settings.  The same rules and JSON
as the Node server."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from qodalis_cli_server_abstractions.config import (
    CliConfigConflictError,
    CliConfigContext,
    CliConfigEntry,
    CliConfigErrors,
    CliConfigSection,
    CliConfigUpdateResult,
    CliConfigValidation,
    CliConfigValue,
    ICliConfigProvider,
    ICliConfigStore,
    ICliConfigWriter,
    InMemoryConfigStore,
    cli_config_store_storage,
    clone_cli_config_value,
    describe_cli_config_store,
    validate_cli_config_value,
)
from qodalis_cli_server_abstractions.config.config_store import CliConfigStoredSection

#: The config provider of the dashboard plugins' settings: one section per installed plugin with settings.
DASHBOARD_PLUGINS_CONFIG_PROVIDER_ID = "dashboard-plugins"
#: The most settings a dashboard plugin may declare.
DASHBOARD_PLUGIN_CONFIG_MAX_ENTRIES = 50
#: The most bytes a dashboard plugin's ``config`` may have as JSON.
DASHBOARD_PLUGIN_CONFIG_MAX_BYTES = 32 * 1024
#: A setting's key: a letter, then letters, digits, ``_``, ``.`` and ``-`` (at most 64).
DASHBOARD_PLUGIN_CONFIG_KEY = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,63}\Z")
#: The types a dashboard plugin's setting may have: every config entry type but ``secret`` (a plugin runs in the
#: browser, it cannot keep one).
DASHBOARD_PLUGIN_CONFIG_TYPES: tuple[str, ...] = ("string", "number", "boolean", "string[]", "enum", "json", "duration", "url")

_DIGITS = re.compile(r"^\d+\Z")


@dataclass
class DashboardPluginConfigEntry:
    """A setting of a dashboard plugin (``qodalis.config.entries`` of its package.json, ``config.entries`` of its
    version record)."""

    key: str
    type: str
    #: ``None`` when none.
    default: CliConfigValue = None
    label: Optional[str] = None
    description: Optional[str] = None
    validation: Optional[CliConfigValidation] = None

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape (as kept in ``installed.json`` and shown by ``admin dashboard info --json``)."""
        out: dict[str, Any] = {"key": self.key}
        if self.label is not None:
            out["label"] = self.label
        if self.description is not None:
            out["description"] = self.description
        out["type"] = self.type
        out["default"] = clone_cli_config_value(self.default)
        if self.validation is not None:
            out["validation"] = self.validation.to_dict()
        return out


@dataclass
class DashboardPluginConfig:
    """A dashboard plugin's settings."""

    entries: list[DashboardPluginConfigEntry] = field(default_factory=list)

    def entry(self, key: str) -> Optional[DashboardPluginConfigEntry]:
        return next((e for e in self.entries if e.key == key), None)

    def to_dict(self) -> dict[str, Any]:
        return {"entries": [e.to_dict() for e in self.entries]}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_regex(pattern: str) -> bool:
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False


def _parse_validation(raw: Any, key: str) -> Optional[CliConfigValidation]:
    """A setting's validation rules, checked and copied (unknown rules dropped).  Raises ``ValueError`` with the reason."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{key}: validation must be an object")
    out = CliConfigValidation()
    present = False
    for rule in ("min", "max"):
        if raw.get(rule) is None and rule not in raw:
            continue
        if not _is_number(raw.get(rule)):
            raise ValueError(f"{key}: validation.{rule} must be a number")
        setattr(out, rule, raw[rule])
        present = True
    if out.min is not None and out.max is not None and out.min > out.max:
        raise ValueError(f"{key}: validation.min is above validation.max")
    if "pattern" in raw:
        pattern = raw["pattern"]
        if not isinstance(pattern, str) or len(pattern) > 500 or not _valid_regex(pattern):
            raise ValueError(f"{key}: validation.pattern must be a regular expression")
        out.pattern = pattern
        present = True
    if "options" in raw:
        options = raw["options"]
        if not isinstance(options, list) or len(options) > 200 or any(not isinstance(o, str) for o in options):
            raise ValueError(f"{key}: validation.options must be a list of strings")
        out.options = list(options)
        present = True
    if "required" in raw:
        if not isinstance(raw["required"], bool):
            raise ValueError(f"{key}: validation.required must be true or false")
        out.required = raw["required"]
        present = True
    return out if present else None


def parse_dashboard_plugin_config(raw: Any) -> Optional[DashboardPluginConfig]:
    """A dashboard plugin's ``config`` as the marketplace publishes it (``{ entries: [...] }``), checked as strictly as
    the marketplace does: at most 50 entries with unique keys (``^[a-zA-Z][a-zA-Z0-9_.-]{0,63}$``), the types of the
    config providers but ``secret`` (a dashboard plugin runs in the browser: it cannot keep one), an ``enum`` with its
    options, defaults that pass their own validation, at most 32 KB of JSON.  ``None`` for no settings (``None`` or no
    entries); raises ``ValueError`` with the reason for a bad one.  Unknown fields are dropped."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("config must be an object")
    try:
        size = len(json.dumps(raw, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise ValueError("config must be JSON") from None
    if size > DASHBOARD_PLUGIN_CONFIG_MAX_BYTES:
        raise ValueError("config is larger than 32 KB")
    raw_entries = raw.get("entries")
    if raw_entries is None:
        raw_entries = []
    if not isinstance(raw_entries, list):
        raise ValueError("config.entries must be a list")
    if len(raw_entries) > DASHBOARD_PLUGIN_CONFIG_MAX_ENTRIES:
        raise ValueError(f"config has more than {DASHBOARD_PLUGIN_CONFIG_MAX_ENTRIES} entries")
    keys: set[str] = set()
    entries: list[DashboardPluginConfigEntry] = []
    for e in raw_entries:
        if not isinstance(e, dict):
            raise ValueError("every config entry must be an object")
        key = e.get("key")
        if not isinstance(key, str) or not DASHBOARD_PLUGIN_CONFIG_KEY.match(key):
            raise ValueError(f"invalid config key: {key}")
        if key in keys:
            raise ValueError(f"duplicate config key: {key}")
        keys.add(key)
        entry_type = e.get("type")
        if entry_type == "secret":
            raise ValueError(f"{key}: secret settings are not allowed (a dashboard plugin runs in the browser)")
        if not isinstance(entry_type, str) or entry_type not in DASHBOARD_PLUGIN_CONFIG_TYPES:
            raise ValueError(f"{key}: unknown type {entry_type}")
        for name in ("label", "description"):
            if e.get(name) is not None and not isinstance(e.get(name), str):
                raise ValueError(f"{key}: {name} must be a string")
        validation = _parse_validation(e.get("validation"), key)
        if entry_type == "enum" and not (validation and validation.options):
            raise ValueError(f"{key}: an enum needs validation.options")
        entry = DashboardPluginConfigEntry(
            key=key,
            type=entry_type,
            default=clone_cli_config_value(e.get("default")),
            label=e["label"] if isinstance(e.get("label"), str) else None,
            description=e["description"] if isinstance(e.get("description"), str) else None,
            validation=validation,
        )
        error = validate_cli_config_value(entry, entry.default)
        if error:
            raise ValueError(f"{key}: the default {error}")
        entries.append(entry)
    return DashboardPluginConfig(entries) if entries else None


def validate_dashboard_plugin_config_values(
    config: Optional[DashboardPluginConfig], values: Mapping[str, Any],
) -> CliConfigErrors:
    """Values given for a plugin's settings (``config`` of an install or update): per-key errors, the config
    providers' validation (``unknown setting``, the type and rules); empty when valid.  ``None`` keeps a setting at
    its default."""
    errors: CliConfigErrors = {}
    for key, value in values.items():
        entry = config.entry(key) if config is not None else None
        if entry is None:
            errors[key] = "unknown setting"
            continue
        error = validate_cli_config_value(entry, value)
        if error:
            errors[key] = error
    return errors


@dataclass
class _PluginSection:
    id: str
    title: str
    config: DashboardPluginConfig
    description: Optional[str] = None


class DashboardPluginsConfigProvider(ICliConfigProvider, ICliConfigWriter):
    """``dashboard-plugins``: the settings of the dashboard plugins installed from the marketplace, one section per
    plugin with settings (its page id, its title), the entries of the installed version with their defaults.
    Writable through the config API, the ``config`` command and MCP like any provider (validation, versions, audit,
    ``config:changed``).  Its store is resolved the usual way: the host's store for ``dashboard-plugins``, else the
    server's default store."""

    id = DASHBOARD_PLUGINS_CONFIG_PROVIDER_ID
    title = "Dashboard plugins"
    description = "Settings of the dashboard plugins installed from the marketplace"
    order = 85
    required_permission = None
    #: The provider has no store of its own (the registry's resolution picks one).
    own_store = None

    def __init__(self) -> None:
        self.storage = cli_config_store_storage()
        self._sections: dict[str, _PluginSection] = {}
        self._cache: dict[str, CliConfigStoredSection] = {}
        self._attached: Optional[ICliConfigStore] = None
        self._fallback: Optional[ICliConfigStore] = None

    @property
    def store(self) -> Optional[ICliConfigStore]:
        """The store the server attached; ``None`` before the provider is registered and the settings loaded."""
        return self._attached

    async def attach_store(self, store: ICliConfigStore) -> None:
        """Called by the server when the provider is registered, with the store it resolved."""
        self._attached = store
        self._cache.clear()

    def _ensure_store(self) -> ICliConfigStore:
        if self._attached is not None:
            return self._attached
        if self._fallback is None:
            self._fallback = InMemoryConfigStore()
        return self._fallback

    async def _stored(self, section_id: str) -> CliConfigStoredSection:
        cached = self._cache.get(section_id)
        if cached is not None:
            return cached
        stored = await self._ensure_store().read(self.id, section_id)
        self._cache[section_id] = stored
        return stored

    def set_section(self, section_id: str, title: str, config: DashboardPluginConfig, description: Optional[str] = None) -> None:
        """Adds or replaces a plugin's section."""
        self._sections[section_id] = _PluginSection(section_id, title, config, description)

    def remove_section(self, section_id: str) -> None:
        """Removes a plugin's section (its stored values stay unless :meth:`clear`\\ ed)."""
        self._sections.pop(section_id, None)

    def has_section(self, section_id: str) -> bool:
        return section_id in self._sections

    def config_of(self, section_id: str) -> Optional[DashboardPluginConfig]:
        """A plugin's settings, as its section declares them."""
        section = self._sections.get(section_id)
        return section.config if section is not None else None

    async def stored_values(self, section_id: str) -> dict[str, CliConfigValue]:
        """The values stored for a section (the settings changed from their defaults), whether it exists or not."""
        return clone_cli_config_value(dict((await self._stored(section_id)).values))

    async def values(self, section_id: str) -> Optional[dict[str, Any]]:
        """A section's effective values (stored over defaults) and its version: ``{ values, version }``; ``None`` for
        no section."""
        section = self._sections.get(section_id)
        if section is None:
            return None
        stored = await self._stored(section_id)
        values = {
            e.key: clone_cli_config_value(stored.values[e.key] if e.key in stored.values else e.default)
            for e in section.config.entries
        }
        return {"values": values, "version": str(stored.version)}

    async def write(self, section_id: str, changes: Mapping[str, Optional[CliConfigValue]]) -> str:
        """Writes changes to a section's stored values (``None`` removes a key) without the config service's checks;
        the new version."""
        stored = await self._ensure_store().write(self.id, section_id, dict(changes))
        self._cache[section_id] = stored
        return str(stored.version)

    async def clear(self, section_id: str) -> tuple[list[str], Optional[str]]:
        """Deletes every value stored for a section: the keys removed and the new version (none: nothing written)."""
        keys = list((await self._stored(section_id)).values.keys())
        if not keys:
            return keys, None
        return keys, await self.write(section_id, {k: None for k in keys})

    @property
    def store_kind(self) -> str:
        """The kind of the store its values are kept in (``memory``, ``file``, ...), as ``config:changed`` carries it."""
        return describe_cli_config_store(self._ensure_store()).kind

    async def get_sections(self, context: Optional[CliConfigContext] = None) -> list[CliConfigSection]:
        result: list[CliConfigSection] = []
        source = self._ensure_store().source
        for section in self._sections.values():
            stored = await self._stored(section.id)
            entries = []
            for e in section.config.entries:
                overridden = e.key in stored.values
                entries.append(CliConfigEntry(
                    key=e.key,
                    type=e.type,
                    label=e.label,
                    description=e.description,
                    value=clone_cli_config_value(stored.values[e.key] if overridden else e.default),
                    default=clone_cli_config_value(e.default),
                    source=source if overridden else "default",
                    mutable=True,
                    validation=CliConfigValidation(**vars(e.validation)) if e.validation is not None else None,
                ))
            result.append(CliConfigSection(
                id=section.id,
                title=section.title,
                description=section.description,
                entries=entries,
                version=str(stored.version),
            ))
        return result

    def validate(
        self, section_id: str, changes: Mapping[str, CliConfigValue], context: Optional[CliConfigContext] = None,
    ) -> CliConfigErrors:
        section = self._sections.get(section_id)
        if section is None:
            return {}
        return {key: "unknown setting" for key in changes if section.config.entry(key) is None}

    async def update(
        self,
        section_id: str,
        changes: Mapping[str, CliConfigValue],
        expected_version: Optional[str] = None,
        context: Optional[CliConfigContext] = None,
    ) -> CliConfigUpdateResult:
        if section_id not in self._sections:
            raise ValueError(f"unknown config section: {self.id}.{section_id}")
        expected: Optional[int] = None
        if expected_version is not None:
            if not _DIGITS.match(expected_version):
                raise CliConfigConflictError(str((await self._stored(section_id)).version))
            expected = int(expected_version)
        stored = await self._ensure_store().write(self.id, section_id, dict(changes), expected)
        self._cache[section_id] = stored
        return CliConfigUpdateResult(applied=list(changes.keys()), requires_restart=False, version=str(stored.version))


__all__ = [
    "DASHBOARD_PLUGINS_CONFIG_PROVIDER_ID",
    "DASHBOARD_PLUGIN_CONFIG_KEY",
    "DASHBOARD_PLUGIN_CONFIG_MAX_BYTES",
    "DASHBOARD_PLUGIN_CONFIG_MAX_ENTRIES",
    "DASHBOARD_PLUGIN_CONFIG_TYPES",
    "DashboardPluginConfig",
    "DashboardPluginConfigEntry",
    "DashboardPluginsConfigProvider",
    "parse_dashboard_plugin_config",
    "validate_dashboard_plugin_config_values",
]
