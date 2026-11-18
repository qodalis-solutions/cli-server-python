"""Qodalis CLI AWS plugin — AWS cloud resource management commands."""

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "☁️"

from .aws_module import AwsModule

__all__ = ["AwsModule"]
