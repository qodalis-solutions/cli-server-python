"""The entry of a data explorer source plugin (``qodalis_cli.plugins``), shared by the official sources: one source,
its connection as settings (a ``secret``: kept server-side, never returned), its name, description, read-only,
timeout and rows.  Without its required connection the plugin needs configuration and adds nothing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .config import CliConfigSettingDefinition, CliConfigValidation
from .data_explorer_types import DataExplorerLanguage, DataExplorerOutputFormat, DataExplorerProviderOptions

_SOURCE_NAME = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"


@dataclass
class DataExplorerSourceEntry:
    """What a source's entry differs in."""

    #: The settings provider's title (``Data explorer: PostgreSQL``).
    title: str
    #: The default source name (``postgres``).
    name: str
    #: The default source description (``PostgreSQL database``).
    description: str
    language: DataExplorerLanguage
    output: DataExplorerOutputFormat
    #: The settings of the connection (one ``required`` secret, and e.g. a database).
    connection: list[CliConfigSettingDefinition] = field(default_factory=list)
    #: ``create(values, context)``: the provider from the settings' values (``{key: value}``).
    create: Callable[[dict[str, Any], Any], Any] = lambda values, context: None


def data_explorer_source_settings(entry: DataExplorerSourceEntry) -> list[CliConfigSettingDefinition]:
    """The source's settings: name, description, its connection, read-only, timeout, rows."""
    restart = {"requires_restart": True}
    return [
        CliConfigSettingDefinition(key="name", label="Source name", description="Shown in the data explorer", type="string",
                                   default=entry.name, validation=CliConfigValidation(required=True, pattern=_SOURCE_NAME), **restart),
        CliConfigSettingDefinition(key="description", label="Description", type="string", default=entry.description, **restart),
        *entry.connection,
        CliConfigSettingDefinition(key="readOnly", label="Read-only", description="Queries may only read", type="boolean",
                                   default=True, **restart),
        CliConfigSettingDefinition(key="timeoutMs", label="Timeout (ms)", type="number", default=30000,
                                   validation=CliConfigValidation(min=1000, max=600000), **restart),
        CliConfigSettingDefinition(key="maxRows", label="Rows at most", type="number", default=1000,
                                   validation=CliConfigValidation(min=1, max=100000), **restart),
    ]


def connection_setting(description: str, key: str = "connectionString", label: str = "Connection string") -> CliConfigSettingDefinition:
    """A required secret connection setting."""
    return CliConfigSettingDefinition(
        key=key, label=label, description=f"{description} (kept as a secret, never returned)", type="secret",
        required=True, requires_restart=True,
    )


async def create_data_explorer_source(context: Any, entry: DataExplorerSourceEntry) -> Optional[Any]:
    """Registers the source's settings and, once configured, the source (``builder.add_data_explorer_provider``)."""
    settings = data_explorer_source_settings(entry)
    provider = await context.settings(title=entry.title, description=f"A source of the data explorer: {entry.description}", settings=settings)
    missing = provider.missing_required()
    if missing:
        return context.needs_configuration(missing)
    values = {s.key: provider.value("settings", s.key) for s in settings}

    def text(key: str, fallback: str) -> str:
        value = values.get(key)
        return str(value).strip() if isinstance(value, str) and value.strip() else fallback

    def number(key: str, fallback: int) -> int:
        value = values.get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else fallback

    context.builder.add_data_explorer_provider(entry.create(values, context), DataExplorerProviderOptions(
        name=text("name", entry.name),
        description=text("description", entry.description),
        language=entry.language,
        default_output_format=entry.output,
        timeout=number("timeoutMs", 30000),
        max_rows=number("maxRows", 1000),
        read_only=values.get("readOnly") is not False,
    ))
    return None


__all__ = [
    "DataExplorerSourceEntry",
    "connection_setting",
    "create_data_explorer_source",
    "data_explorer_source_settings",
]
