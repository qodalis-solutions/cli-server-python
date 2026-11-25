"""A provider of settings described in code, kept in a config store."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from .config_store import CliConfigStoredSection, ICliConfigStore, InMemoryConfigStore
from .config_types import (
    CLI_CONFIG_KEY,
    CliConfigConflictError,
    CliConfigContext,
    CliConfigEntry,
    CliConfigErrors,
    CliConfigSection,
    CliConfigUpdateResult,
    CliConfigValidation,
    CliConfigValue,
    ICliConfigProvider,
    ICliConfigWriter,
)
from .config_validation import clone_cli_config_value, infer_cli_config_entry_type


@dataclass
class CliConfigSettingDefinition:
    """A setting of a :class:`CliStoredConfigProvider`."""

    key: str
    type: str
    label: Optional[str] = None
    description: Optional[str] = None
    #: The value when nothing is stored (what the host configured); ``None`` when none.
    default: CliConfigValue = None
    #: Where ``default`` comes from (e.g. ``environment`` when the host read it from a variable); default ``default``.
    source: Optional[str] = None
    #: Can be changed (default true).  A read-only setting shows ``value()`` (or ``default``).
    mutable: bool = True
    requires_restart: bool = False
    #: Never returned; every ``secret`` is sensitive.
    sensitive: bool = False
    validation: Optional[CliConfigValidation] = None
    editor: Optional[str] = None
    #: The live value of a read-only setting (read on every ``get_sections``).
    value: Optional[Callable[[], CliConfigValue]] = None
    #: A further rule on a new value (after the type and ``validation``); the message, or ``None``.
    validate: Optional[Callable[[CliConfigValue], Optional[str]]] = None
    #: Makes the effective value take effect (the stored value, or ``default``
    #: after a reset): called for every stored value when the store is
    #: attached, and after every change.
    apply: Optional[Callable[[CliConfigValue], None]] = None

    def __post_init__(self) -> None:
        self.validation = CliConfigValidation.of(self.validation)


@dataclass
class CliConfigSectionDefinition:
    """A section of a :class:`CliStoredConfigProvider`."""

    id: str
    settings: list[CliConfigSettingDefinition] = field(default_factory=list)
    title: Optional[str] = None
    description: Optional[str] = None
    #: Keys not in ``settings`` may be added and removed (``None``): a free key/value section.
    allow_custom_keys: bool = False


_DIGITS = re.compile(r"^\d+\Z")


class CliStoredConfigProvider(ICliConfigProvider, ICliConfigWriter):
    """A provider of settings described in code, whose changes are kept in an
    :class:`ICliConfigStore`: the server attaches its store when the provider
    is registered (``builder.set_config_store``, in memory by default) and the
    stored values are applied then.  Built-in providers and plugins use it; a
    host can too::

        builder.add_config_provider(CliStoredConfigProvider(
            id="billing", title="Billing",
            sections=[CliConfigSectionDefinition(id="invoices", settings=[
                CliConfigSettingDefinition(key="dueDays", type="number", default=30,
                    validation=CliConfigValidation(min=1, max=90),
                    apply=lambda v: setattr(billing, "due_days", v)),
                CliConfigSettingDefinition(key="apiKey", type="secret"),
            ])],
        ))
    """

    def __init__(
        self,
        id: str,
        title: str,
        sections: Sequence[CliConfigSectionDefinition],
        description: Optional[str] = None,
        order: Optional[int] = None,
        required_permission: Optional[str] = None,
    ) -> None:
        self.id = id
        self.title = title
        self.description = description
        self.order = order
        self.required_permission = required_permission
        ids: set[str] = set()
        for section in sections:
            if not CLI_CONFIG_KEY.match(section.id) or section.id in ids:
                raise ValueError(f"invalid or duplicate config section: {section.id}")
            ids.add(section.id)
            keys: set[str] = set()
            for setting in section.settings:
                if not CLI_CONFIG_KEY.match(setting.key) or setting.key in keys:
                    raise ValueError(f"invalid or duplicate config key: {section.id}.{setting.key}")
                keys.add(setting.key)
        self._sections = list(sections)
        self._store: Optional[ICliConfigStore] = None
        self._cache: dict[str, CliConfigStoredSection] = {}

    async def attach_store(self, store: ICliConfigStore) -> None:
        """Uses ``store`` and applies the values stored there.  Called by the
        server when the provider is registered; without it the provider keeps
        its changes in memory.  Raises when the store cannot be read."""
        self._store = store
        self._cache.clear()
        for section in self._sections:
            stored = await store.read(self.id, section.id)
            self._cache[section.id] = stored
            for setting in section.settings:
                if setting.mutable and setting.key in stored.values and setting.apply is not None:
                    setting.apply(clone_cli_config_value(stored.values[setting.key]))

    def _ensure_store(self) -> ICliConfigStore:
        if self._store is None:
            self._store = InMemoryConfigStore()
        return self._store

    @property
    def store(self) -> ICliConfigStore:
        """The store the provider's changes are kept in (in memory until the server attaches its own)."""
        return self._ensure_store()

    async def _stored(self, section_id: str) -> CliConfigStoredSection:
        cached = self._cache.get(section_id)
        if cached is not None:
            return cached
        stored = await self._ensure_store().read(self.id, section_id)
        self._cache[section_id] = stored
        return stored

    def _section(self, section_id: str) -> Optional[CliConfigSectionDefinition]:
        return next((s for s in self._sections if s.id == section_id), None)

    def value(self, section_id: str, key: str) -> CliConfigValue:
        """A setting's effective value: the stored one, else its default (or
        the live value of a read-only setting).  Secrets included: for the
        host's own code, never for clients.  ``None`` for an unknown key."""
        section = self._section(section_id)
        setting = next((s for s in section.settings if s.key == key), None) if section else None
        cached = self._cache.get(section_id)
        stored = cached.values if cached is not None else None
        if setting is not None:
            if not setting.mutable:
                return setting.value() if setting.value is not None else setting.default
            if stored is not None and key in stored:
                return clone_cli_config_value(stored[key])
            return clone_cli_config_value(setting.default)
        if section is not None and section.allow_custom_keys and stored is not None and key in stored:
            return clone_cli_config_value(stored[key])
        return None

    async def get_sections(self, context: Optional[CliConfigContext] = None) -> list[CliConfigSection]:
        result: list[CliConfigSection] = []
        source = self._ensure_store().source
        for section in self._sections:
            stored = await self._stored(section.id)
            entries: list[CliConfigEntry] = []
            for setting in section.settings:
                overridden = setting.mutable and setting.key in stored.values
                if not setting.mutable and setting.value is not None:
                    value = setting.value()
                elif overridden:
                    value = stored.values[setting.key]
                else:
                    value = setting.default
                sensitive = setting.sensitive or setting.type == "secret"
                entries.append(CliConfigEntry(
                    key=setting.key,
                    type=setting.type,
                    label=setting.label,
                    description=setting.description,
                    value=None if sensitive else clone_cli_config_value(value),
                    default=None if sensitive else clone_cli_config_value(setting.default),
                    is_set=(value is not None and value != "") if sensitive else None,
                    source=source if overridden else (setting.source or "default"),
                    mutable=setting.mutable,
                    requires_restart=setting.requires_restart,
                    sensitive=sensitive,
                    validation=CliConfigValidation(**vars(setting.validation)) if setting.validation else None,
                    editor=setting.editor,
                ))
            if section.allow_custom_keys:
                known = {s.key for s in section.settings}
                for key, value in stored.values.items():
                    if key in known:
                        continue
                    entries.append(CliConfigEntry(
                        key=key,
                        type=infer_cli_config_entry_type(value),
                        value=clone_cli_config_value(value),
                        default=None,
                        source=source,
                        mutable=True,
                    ))
            writable = section.allow_custom_keys or any(s.mutable for s in section.settings)
            result.append(CliConfigSection(
                id=section.id,
                title=section.title,
                description=section.description,
                entries=entries,
                version=str(stored.version) if writable else None,
                allow_custom_keys=section.allow_custom_keys,
            ))
        return result

    def validate(
        self, section_id: str, changes: Mapping[str, CliConfigValue], context: Optional[CliConfigContext] = None,
    ) -> CliConfigErrors:
        errors: CliConfigErrors = {}
        section = self._section(section_id)
        if section is None:
            return errors
        for key, value in changes.items():
            setting = next((s for s in section.settings if s.key == key), None)
            if setting is None:
                if not section.allow_custom_keys:
                    errors[key] = "unknown setting"
                elif not CLI_CONFIG_KEY.match(key):
                    errors[key] = "invalid key: use letters, digits, _, - and ."
                continue
            if not setting.mutable:
                errors[key] = "read-only"
                continue
            if value is not None and setting.validate is not None:
                error = setting.validate(value)
                if error:
                    errors[key] = error
        return errors

    async def update(
        self,
        section_id: str,
        changes: Mapping[str, CliConfigValue],
        expected_version: Optional[str] = None,
        context: Optional[CliConfigContext] = None,
    ) -> CliConfigUpdateResult:
        section = self._section(section_id)
        if section is None:
            raise ValueError(f"unknown config section: {self.id}.{section_id}")
        expected: Optional[int] = None
        if expected_version is not None:
            if not _DIGITS.match(expected_version):
                raise CliConfigConflictError(str((await self._stored(section_id)).version))
            expected = int(expected_version)
        stored = await self._ensure_store().write(self.id, section_id, dict(changes), expected)
        self._cache[section_id] = stored
        applied = list(changes.keys())
        requires_restart = False
        for key in applied:
            setting = next((s for s in section.settings if s.key == key), None)
            if setting is None:
                continue
            if setting.requires_restart:
                requires_restart = True
            if setting.apply is not None:
                setting.apply(self.value(section_id, key))
        return CliConfigUpdateResult(applied=applied, requires_restart=requires_restart, version=str(stored.version))


def create_custom_config_provider(
    id: str = "custom",
    title: str = "Custom",
    description: str = "Free settings of the host application, kept in the config store",
    order: int = 1000,
) -> CliStoredConfigProvider:
    """The ``custom`` provider: one free key/value section (``custom``) on the
    server's config store, for the host's own settings.  Values are strings,
    numbers, booleans, string lists or JSON."""
    return CliStoredConfigProvider(
        id=id,
        title=title,
        description=description,
        order=order,
        sections=[CliConfigSectionDefinition(id="custom", title="Custom settings", allow_custom_keys=True)],
    )


__all__ = [
    "CliConfigSettingDefinition",
    "CliConfigSectionDefinition",
    "CliStoredConfigProvider",
    "create_custom_config_provider",
]
