"""The weather plugin's entry point (``qodalis_cli.plugins``): the ``weather`` commands, with the default place as a
setting (``config set weather.settings.defaultLocation Paris``), read on every command.  Installed from the dashboard
or added in code: ``builder.add_plugin(qodalis_cli_weather.plugin, {"defaultLocation": "Paris"})``."""

from __future__ import annotations

from typing import Any

from qodalis_cli_server_abstractions import CliPluginEntryInfo
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition, CliConfigValidation

from .weather_module import DEFAULT_WEATHER_LOCATION, WeatherModule

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(id="weather", package="qodalis-cli-server-plugin-weather", title="Weather", icon="🌤️")


def settings() -> list[CliConfigSettingDefinition]:
    """Its settings (code options of the same keys are their defaults)."""
    return [
        CliConfigSettingDefinition(
            key="defaultLocation", label="Default location", description="The place when a command names none",
            type="string", default=DEFAULT_WEATHER_LOCATION, validation=CliConfigValidation(required=True, max=100),
        ),
    ]


async def create_plugin(context: Any) -> Any:
    provider = await context.settings(title="Weather", description="The weather commands (wttr.in)", settings=settings())
    return WeatherModule(default_location=lambda: str(provider.value("settings", "defaultLocation") or DEFAULT_WEATHER_LOCATION))
