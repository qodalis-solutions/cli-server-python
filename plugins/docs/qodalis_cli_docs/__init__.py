"""Qodalis CLI docs plugin — markdown guides from folders or code, with front
matter and variables, served as JSON for the portal site."""

from __future__ import annotations

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "📚"

from .cli_docs_builder import DEFAULT_BASE_PATH, CliDocsBuilder, CliDocsPlugin
from .pages import DocsPage, parse_page

__all__ = [
    "CliDocsBuilder",
    "CliDocsPlugin",
    "DEFAULT_BASE_PATH",
    "DocsPage",
    "parse_page",
]
