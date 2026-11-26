"""The AWS plugin's entry point (``qodalis_cli.plugins``): the ``aws`` commands.  The region, profile and access keys
are settings (the keys ``secret`` settings: kept server-side, never returned), applied at start; without keys the AWS
default credential chain is used.  ``aws configure set`` still changes them until the next restart."""

from __future__ import annotations

from typing import Any

from qodalis_cli_server_abstractions import CliPluginEntryInfo
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition

from .aws_module import AwsModule
from .processors.aws_command_processor import AwsCommandProcessor

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(id="aws", package="qodalis-cli-server-plugin-aws", title="AWS", icon="☁️")


def settings() -> list[CliConfigSettingDefinition]:
    """Its settings (code options of the same keys are their defaults)."""
    return [
        CliConfigSettingDefinition(key="region", label="Region", description="e.g. eu-north-1; empty: the default chain",
                                   type="string", default="", requires_restart=True),
        CliConfigSettingDefinition(key="profile", label="Profile", description="A profile of the shared AWS config; empty: none",
                                   type="string", default="", requires_restart=True),
        CliConfigSettingDefinition(key="accessKeyId", label="Access key id", description="Empty: the AWS default credential chain",
                                   type="secret", requires_restart=True),
        CliConfigSettingDefinition(key="secretAccessKey", label="Secret access key", type="secret", requires_restart=True),
    ]


async def create_plugin(context: Any) -> Any:
    provider = await context.settings(title="AWS", description="The aws commands: region and credentials", settings=settings())

    def text(key: str) -> str:
        return str(provider.value("settings", key) or "").strip()

    module = AwsModule()
    aws = next((p for p in module.processors if isinstance(p, AwsCommandProcessor)), None)
    if aws is not None:
        config = aws.get_config_service()
        if text("region"):
            config.set_region(text("region"))
        if text("profile"):
            config.set_profile(text("profile"))
        if text("accessKeyId") and text("secretAccessKey"):
            config.set_credentials(text("accessKeyId"), text("secretAccessKey"))
    return module
