"""Qodalis CLI public-api plugin — a host's public REST API: routes guarded by
``host`` API tokens with per-account quotas (``X-RateLimit-*`` headers),
usage per token, the public OpenAPI document and Swagger UI."""

from __future__ import annotations

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🔑"

from .cli_public_api_builder import (
    DEFAULT_BASE_PATH,
    CliPublicApiBuilder,
    CliPublicApiPlugin,
)
from .api_command import TOKENS_READ_PERMISSION, CliApiCommandProcessor, PublicApiModule, format_account_usage, format_quota
from .quota import PublicApiQuota, QuotaResolver
from .usage_service import PublicApiAccountUsage, PublicApiTokenUsage, PublicApiUsageService
from .usage_store import InMemoryPublicApiUsageStore, IPublicApiUsageStore

__all__ = [
    "TOKENS_READ_PERMISSION",
    "CliApiCommandProcessor",
    "PublicApiModule",
    "format_account_usage",
    "format_quota",
    "CliPublicApiBuilder",
    "CliPublicApiPlugin",
    "DEFAULT_BASE_PATH",
    "IPublicApiUsageStore",
    "InMemoryPublicApiUsageStore",
    "PublicApiAccountUsage",
    "PublicApiQuota",
    "PublicApiTokenUsage",
    "PublicApiUsageService",
    "QuotaResolver",
]
