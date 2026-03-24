"""Weather CLI module providing current conditions and forecast commands."""

from __future__ import annotations

import asyncio

import urllib.parse
import urllib.request
import json
from typing import Sequence

from qodalis_cli import (
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliModule,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
    ICliCommandProcessor,
)


def _get_location(command: CliProcessCommand) -> str:
    """Extract the location from command args or value, defaulting to London."""
    if "location" in command.args:
        return str(command.args["location"])
    if command.value:
        return command.value
    return "London"


def _fetch_weather_data(location: str) -> dict:
    """Fetch weather data from wttr.in for the given location."""
    url = f"https://wttr.in/{urllib.parse.quote(location)}?format=j1"
    req = urllib.request.Request(url, headers={"User-Agent": "qodalis-cli/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _format_current(location: str) -> str:
    """Format current weather conditions as a human-readable string."""
    try:
        data = _fetch_weather_data(location)
        current = data["current_condition"][0]
        area = data["nearest_area"][0]
        city = area["areaName"][0]["value"]
        country = area["country"][0]["value"]

        return "\n".join([
            f"Weather for {city}, {country}",
            f"  Condition:   {current['weatherDesc'][0]['value']}",
            f"  Temperature: {current['temp_C']}\u00b0C (feels like {current['FeelsLikeC']}\u00b0C)",
            f"  Humidity:    {current['humidity']}%",
            f"  Wind:        {current['windspeedKmph']} km/h {current['winddir16Point']}",
            f"  Visibility:  {current['visibility']} km",
            f"  Pressure:    {current['pressure']} hPa",
        ])
    except Exception as exc:
        return f"Failed to fetch weather data: {exc}"


def _format_forecast(location: str) -> str:
    """Format a 3-day weather forecast as a human-readable string."""
    try:
        data = _fetch_weather_data(location)
        area = data["nearest_area"][0]
        city = area["areaName"][0]["value"]
        country = area["country"][0]["value"]

        lines = [f"3-day forecast for {city}, {country}\n"]
        for day in data["weather"]:
            desc = day["hourly"][4]["weatherDesc"][0]["value"]
            rain = day["hourly"][4]["chanceofrain"]
            lines.append(
                f"  {day['date']}: {desc}, {day['mintempC']}\u00b0C - {day['maxtempC']}\u00b0C, rain {rain}%"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Failed to fetch forecast data: {exc}"


_location_param = CliCommandParameterDescriptor(
    name="location",
    description="Location to get weather for (city name)",
    required=False,
    type="string",
    aliases=["-l"],
    default_value="London",
)


class _WeatherCurrentProcessor(CliCommandProcessor):
    """Processor for the ``weather current`` sub-command."""

    @property
    def command(self) -> str:
        return "current"

    @property
    def description(self) -> str:
        return "Shows current weather conditions"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [_location_param]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return _format_current(_get_location(command))


class _WeatherForecastProcessor(CliCommandProcessor):
    """Processor for the ``weather forecast`` sub-command."""

    @property
    def command(self) -> str:
        return "forecast"

    @property
    def description(self) -> str:
        return "Shows a 3-day weather forecast"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [_location_param]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return _format_forecast(_get_location(command))


class _CliWeatherCommandProcessor(CliCommandProcessor):
    """Root processor for the ``weather`` command with sub-commands."""

    @property
    def command(self) -> str:
        return "weather"

    @property
    def description(self) -> str:
        return "Shows weather information for a location"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [_location_param]

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return [_WeatherCurrentProcessor(), _WeatherForecastProcessor()]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return _format_current(_get_location(command))


class WeatherModule(CliModule):
    """CLI module that registers the weather command and its sub-commands."""

    @property
    def name(self) -> str:
        return "weather"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Provides weather information commands"

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return [_CliWeatherCommandProcessor()]
