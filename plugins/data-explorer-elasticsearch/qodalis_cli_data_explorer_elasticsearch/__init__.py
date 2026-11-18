"""Qodalis CLI Data Explorer Elasticsearch plugin — Elasticsearch provider."""

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🔍"

from .elasticsearch_provider import ElasticsearchDataExplorerProvider

__all__ = ["ElasticsearchDataExplorerProvider"]
