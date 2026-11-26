"""The public-api plugin's entry point (``qodalis_cli.plugins``, ``builder.add_plugin``): the host's public API with host
tokens, per-account quotas and usage, and its OpenAPI document with Swagger UI at ``basePath``.  It builds on the
users plugin (the ``users`` service: added in code, mounted, or installed from the marketplace); without it it needs
configuration.  The quota from its settings applies to every account (``quota``, a function, from code only, wins);
the host's routes take the guard from the ``public-api`` service
(``builder.plugin_services.get("public-api").guard("rates:read")``)."""

from __future__ import annotations

import json
import os
from typing import Any

from qodalis_cli_server_abstractions import CliPluginEntryInfo
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition, CliConfigValidation

from .cli_public_api_builder import DEFAULT_BASE_PATH, DEFAULT_RETENTION_DAYS, DEFAULT_TITLE, CliPublicApiBuilder
from .quota import PublicApiQuota

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(id="public-api", package="qodalis-cli-server-plugin-public-api", title="Public API", icon="🌐")


def settings() -> list[CliConfigSettingDefinition]:
    """Its settings (code options of the same keys are their defaults; ``quota``, ``usage_store``, ``openapi`` from code)."""
    restart = {"requires_restart": True}
    return [
        CliConfigSettingDefinition(key="basePath", label="Path", description="Where the OpenAPI document and Swagger UI are served",
                                   type="string", default=DEFAULT_BASE_PATH,
                                   validation=CliConfigValidation(required=True, pattern=r"^(/[A-Za-z0-9._~-]+)+$"), **restart),
        CliConfigSettingDefinition(key="title", label="Title", type="string", default=DEFAULT_TITLE,
                                   validation=CliConfigValidation(required=True, max=100), **restart),
        CliConfigSettingDefinition(key="openApiFile", label="OpenAPI document",
                                   description="A JSON file (relative paths from the plugin's data directory); empty: an empty document",
                                   type="string", default="", validation=CliConfigValidation(max=500), **restart),
        CliConfigSettingDefinition(key="serverUrl", label="Server URL", description="The document's servers URL; empty: the request's",
                                   type="string", default="", validation=CliConfigValidation(pattern=r"^(|https?://\S+)$"), **restart),
        CliConfigSettingDefinition(key="requestsPerDay", label="Requests per day", description="Per account; 0: unlimited",
                                   type="number", default=0, validation=CliConfigValidation(min=0), **restart),
        CliConfigSettingDefinition(key="requestsPerMinute", label="Requests per minute", description="Per account; 0: unlimited",
                                   type="number", default=0, validation=CliConfigValidation(min=0), **restart),
        CliConfigSettingDefinition(key="usageRetentionDays", label="Usage kept (days)", type="number", default=DEFAULT_RETENTION_DAYS,
                                   validation=CliConfigValidation(min=1, max=3650), **restart),
    ]


def _openapi_file(path: str) -> Any:
    def read() -> dict[str, Any]:
        try:
            with open(path, encoding="utf-8") as f:
                document = json.load(f)
            return document if isinstance(document, dict) else {}
        except (OSError, ValueError):
            return {}

    return read


async def create_plugin(context: Any) -> Any:
    provider = await context.settings(title="Public API", description="The host's public API: tokens, quotas, its document", settings=settings())
    users = context.options.get("users") or context.services.get("users")
    if users is None:
        return context.needs_configuration("the users plugin (install users, or add it before public-api)")

    def text(key: str) -> str:
        return str(provider.value("settings", key) or "").strip()

    def num(key: str) -> int:
        value = provider.value("settings", key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    builder = (
        CliPublicApiBuilder()
        .set_users(users)
        .set_base_path(text("basePath") or DEFAULT_BASE_PATH)
        .set_title(text("title") or DEFAULT_TITLE)
        .set_usage_retention_days(max(1, num("usageRetentionDays") or DEFAULT_RETENTION_DAYS))
    )
    openapi = context.options.get("openapi") or context.options.get("openApi")
    if openapi is not None:
        builder.set_openapi(openapi)
    elif text("openApiFile"):
        file = text("openApiFile")
        builder.set_openapi(_openapi_file(file if os.path.isabs(file) else os.path.join(context.data_dir, file)))
    if text("serverUrl"):
        builder.set_server_url(text("serverUrl"))
    quota = context.options.get("quota")
    per_day, per_minute = num("requestsPerDay"), num("requestsPerMinute")
    if callable(quota):
        builder.set_quota(quota)
    elif per_day > 0 or per_minute > 0:
        builder.set_quota(lambda _caller: PublicApiQuota(per_day if per_day > 0 else None, per_minute if per_minute > 0 else None))
    usage_store = context.options.get("usage_store") or context.options.get("usageStore")
    if usage_store is not None:
        builder.set_usage_store(usage_store)
    result = builder.build()
    context.services.set("public-api", result)
    return result
