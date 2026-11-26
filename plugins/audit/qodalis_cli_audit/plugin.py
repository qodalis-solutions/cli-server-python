"""The audit plugin's entry point (``qodalis_cli.plugins``): the audit log (``/api/v1/qcli/audit``) and the dashboard's
Audit page, kept in memory (its size a setting).  On a server protected by the users plugin the host already has it
(the users plugin brings it): the plugin's copy then declines to be mounted."""

from __future__ import annotations

from typing import Any

from qodalis_cli_server_abstractions import CliPluginEntryInfo
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition, CliConfigValidation

from .cli_audit_builder import CliAuditBuilder
from .in_memory_audit_store import MAX_ENTRIES, InMemoryAuditStore

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(id="audit", package="qodalis-cli-server-plugin-audit", title="Audit", icon="📋")


def settings() -> list[CliConfigSettingDefinition]:
    """Its settings (code options of the same keys are their defaults)."""
    return [
        CliConfigSettingDefinition(
            key="maxEntries", label="Entries kept", description="The oldest are dropped beyond it", type="number",
            default=MAX_ENTRIES, requires_restart=True, validation=CliConfigValidation(min=100, max=1_000_000),
        ),
    ]


async def create_plugin(context: Any) -> Any:
    provider = await context.settings(title="Audit", description="The audit log, kept in memory", settings=settings())
    size = provider.value("settings", "maxEntries")
    return CliAuditBuilder().set_store(InMemoryAuditStore(int(size) if isinstance(size, (int, float)) else MAX_ENTRIES)).build()
