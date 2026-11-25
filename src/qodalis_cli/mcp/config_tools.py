"""``config_list``, ``config_get`` and ``config_set``: the server's config providers as MCP tools."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from qodalis_cli_server_abstractions.config import CliConfigError, CliConfigPermissions, ICliConfigService

from ..config.config_command import config_context_of_auth
from .core import TIER_BUILTIN, McpCallContext, McpTool, McpToolResult, object_schema


def _json(value: Any) -> McpToolResult:
    return McpToolResult(text=json.dumps(value, indent=2, ensure_ascii=False), structured=value)


async def _run(fn: Callable[[], Awaitable[McpToolResult]]) -> McpToolResult:
    try:
        return await fn()
    except CliConfigError as exc:
        body = exc.to_dict()
        return McpToolResult(text=json.dumps(body, indent=2, ensure_ascii=False), structured=body, is_error=True)


class ConfigMcpToolProvider:
    """``config_list``, ``config_get`` (read-only, ``config:read``) and
    ``config_set`` (``config:write``, not read-only: hidden by the endpoint's
    ``read_only`` option and the MCP client's ``QCLI_READ_ONLY``).  Secrets
    are never returned."""

    mcp_tier = TIER_BUILTIN
    name = "config"

    def __init__(self, service: ICliConfigService) -> None:
        self.service = service

    def get_tools(self, context: McpCallContext) -> list[McpTool]:
        service = self.service

        async def config_list(args: dict[str, Any], ctx: McpCallContext) -> McpToolResult:
            async def run() -> McpToolResult:
                providers = await service.list(config_context_of_auth(ctx.auth, "mcp"))
                return _json({"defaultStore": service.describe_default_store().to_dict(), "providers": [
                    {
                        "id": p["id"],
                        "title": p["title"],
                        "description": p["description"],
                        "writable": p["writable"],
                        "storage": p.get("storage"),
                        **({"store": p["store"]} if p.get("store") else {}),
                        "sections": [
                            {
                                "id": s["id"], "title": s["title"], "mutable": s["mutable"],
                                "version": s["version"], "keys": len(s["entries"]),
                            }
                            for s in p["sections"]
                        ],
                    }
                    for p in providers
                ]})
            return await _run(run)

        async def config_get(args: dict[str, Any], ctx: McpCallContext) -> McpToolResult:
            async def run() -> McpToolResult:
                provider = await service.get(str(args.get("provider")), config_context_of_auth(ctx.auth, "mcp"))
                section_id = args.get("section")
                if section_id is None:
                    return _json({"provider": provider})
                section = next((s for s in provider["sections"] if s["id"] == section_id), None)
                if section is None:
                    return McpToolResult.error(f"unknown config section: {provider['id']}.{section_id}")
                key = args.get("key")
                if key is None:
                    return _json({"provider": provider["id"], "section": section})
                entry = next((e for e in section["entries"] if e["key"] == key), None)
                if entry is None:
                    return McpToolResult.error(f"unknown setting: {provider['id']}.{section['id']}.{key}")
                return _json({"provider": provider["id"], "section": section["id"], "version": section["version"], "entry": entry})
            return await _run(run)

        async def config_set(args: dict[str, Any], ctx: McpCallContext) -> McpToolResult:
            async def run() -> McpToolResult:
                request: dict[str, Any] = {"changes": args.get("changes")}
                if isinstance(args.get("version"), str):
                    request["version"] = args["version"]
                result = await service.update(
                    str(args.get("provider")), str(args.get("section")), request, config_context_of_auth(ctx.auth, "mcp"),
                )
                return _json(result)
            return await _run(run)

        return [
            McpTool(
                name="config_list",
                description=(
                    "The server's config providers (where each reads its settings from, and the store a stored one "
                    "saves changes to), the default store, and their sections (ids, titles, whether writable, versions); "
                    "read a section with config_get"
                ),
                input_schema=object_schema(),
                call=config_list,
                required_permissions=[CliConfigPermissions.READ],
                annotations={"readOnlyHint": True},
            ),
            McpTool(
                name="config_get",
                description=(
                    "A config provider, one of its sections, or one setting (provider, section, key): values, "
                    "defaults, sources and validation rules; secrets show only isSet"
                ),
                input_schema=object_schema(
                    {
                        "provider": {"type": "string", "description": "Provider id (config_list)"},
                        "section": {"type": "string", "description": "Section id (optional)"},
                        "key": {"type": "string", "description": "Setting key (optional, with section)"},
                    },
                    ["provider"],
                ),
                call=config_get,
                required_permissions=[CliConfigPermissions.READ],
                annotations={"readOnlyHint": True},
            ),
            McpTool(
                name="config_set",
                description=(
                    "Changes settings of a config section: changes maps keys to new values (null resets to the "
                    "default, clears a secret); pass the version from config_get to refuse a stale change. "
                    "Answers per-key errors when invalid"
                ),
                input_schema=object_schema(
                    {
                        "provider": {"type": "string", "description": "Provider id"},
                        "section": {"type": "string", "description": "Section id"},
                        "changes": {"type": "object", "description": "Key to new value (null resets)"},
                        "version": {
                            "type": "string", "description": "The section version the change is based on (optional)",
                        },
                    },
                    ["provider", "section", "changes"],
                ),
                call=config_set,
                required_permissions=[CliConfigPermissions.WRITE],
                annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
            ),
        ]
