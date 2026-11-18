"""Qodalis CLI weather plugin — weather information commands."""

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🌤️"

from .weather_module import WeatherModule

__all__ = ["WeatherModule"]
