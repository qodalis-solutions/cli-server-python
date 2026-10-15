"""Command tools: one MCP tool per CLI command (processor tree node)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..abstractions import CliProcessCommand, ICliCommandProcessor
from ..services.cli_command_executor_service import ICliCommandExecutorService
from ..services.cli_command_registry import ICliCommandRegistry
from .core import TIER_COMMANDS, McpCallContext, McpTool, McpToolResult, object_schema

logger = logging.getLogger(__name__)

COMMANDS_EXECUTE = "commands:execute"
TOOL_NAME_MAX = 64
_NAME_INVALID_CHARS = re.compile(r"[^a-z0-9_-]")
_JSON_TYPES = {"string": "string", "number": "number", "boolean": "boolean", "array": "array"}


def command_tool_name(path: list[str]) -> str:
    """``weather current`` → ``weather_current`` (lower-cased, characters
    outside ``[a-z0-9_-]`` replaced by ``-``, at most 64 characters)."""
    return _NAME_INVALID_CHARS.sub("-", "_".join(path).lower())[:TOOL_NAME_MAX]


def takes_value(proc: ICliCommandProcessor) -> bool:
    """Python processors declare ``value_required`` ``True`` (required
    positional value), ``False`` (optional value) or ``None`` (no value)."""
    return getattr(proc, "value_required", None) is not None


def command_input_schema(proc: ICliCommandProcessor) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in getattr(proc, "parameters", None) or []:
        name = getattr(param, "name", None)
        if not isinstance(name, str) or not name or name in properties:
            continue
        json_type = _JSON_TYPES.get(str(getattr(param, "type", "string") or "string").lower(), "string")
        prop: dict[str, Any] = {"type": json_type}
        if json_type == "array":
            prop["items"] = {"type": "string"}
        description = getattr(param, "description", None)
        if description:
            prop["description"] = description
        default = getattr(param, "default_value", None)
        if default is not None:
            prop["default"] = default
        properties[name] = prop
        if getattr(param, "required", False):
            required.append(name)
    if takes_value(proc) and "value" not in properties:
        properties["value"] = {"type": "string", "description": "Positional input of the command"}
        if proc.value_required:
            required.append("value")
    return object_schema(properties, required)


def render_outputs(outputs: list[dict[str, Any]]) -> str:
    """Outputs as plain text: text as-is, tables header + rows joined with
    `` | ``, lists one item per line, json pretty-printed, key-value as
    ``key: value``."""
    lines: list[str] = []
    for out in outputs:
        kind = out.get("type")
        if kind == "text":
            lines.append(str(out.get("value", "")))
        elif kind == "table":
            lines.append(" | ".join(str(h) for h in out.get("headers") or []))
            lines.extend(" | ".join(str(c) for c in row) for row in out.get("rows") or [])
        elif kind == "list":
            lines.extend(str(item) for item in out.get("items") or [])
        elif kind == "json":
            lines.append(json.dumps(out.get("value"), indent=2, ensure_ascii=False, default=str))
        elif kind == "key-value":
            lines.extend(f"{e.get('key')}: {e.get('value')}" for e in out.get("entries") or [])
        else:
            lines.append(json.dumps(out, ensure_ascii=False, default=str))
    return "\n".join(lines)


def _raw_command(path: list[str], value: str | None, args: dict[str, Any]) -> str:
    parts = list(path)
    if value:
        parts.append(value)
    for key, val in args.items():
        if isinstance(val, bool):
            parts.append(f"--{key}" if val else f"--{key}=false")
        elif isinstance(val, list):
            parts.append(f"--{key}={','.join(map(str, val))}")
        else:
            parts.append(f"--{key}={val}")
    return " ".join(parts)


class CommandsMcpToolProvider:
    """Walks the command registry: a node becomes a tool when it has no
    sub-processors, declares parameters or takes a value.  Calls run through
    the same executor as ``/execute``."""

    mcp_tier = TIER_COMMANDS
    name = "commands"

    def __init__(self, registry: ICliCommandRegistry, executor: ICliCommandExecutorService) -> None:
        self.registry = registry
        self.executor = executor

    def get_tools(self, context: McpCallContext) -> list[McpTool]:
        tools: list[McpTool] = []
        seen: set[str] = set()

        def visit(proc: ICliCommandProcessor, path: list[str], chain_perms: list[str]) -> None:
            path = [*path, proc.command]
            perms = [*chain_perms, *(getattr(proc, "required_permissions", None) or [])]
            subs = list(proc.processors or [])
            if not subs or proc.parameters or takes_value(proc):
                name = command_tool_name(path)
                if name in seen:
                    logger.warning("MCP command tool name collision: '%s' (%s) ignored", name, " ".join(path))
                else:
                    seen.add(name)
                    tools.append(self._tool(proc, path, perms, name))
            for sub in subs:
                visit(sub, path, perms)

        for root in self.registry.processors:
            visit(root, [], [COMMANDS_EXECUTE])
        return tools

    def _tool(self, proc: ICliCommandProcessor, path: list[str], perms: list[str], name: str) -> McpTool:
        declared = {getattr(p, "name", None) for p in proc.parameters or []}

        async def call(arguments: dict[str, Any], context: McpCallContext) -> McpToolResult:
            args = dict(arguments)
            value = args.pop("value", None) if takes_value(proc) and "value" not in declared else None
            command = CliProcessCommand(
                command=path[0],
                raw_command=_raw_command(path, value, args),
                value=value,
                args=args,
                chain_commands=path[1:],
            )
            response = await self.executor.execute_async(command, cancellation_event=None)
            outputs = [o.model_dump(by_alias=True, exclude_none=True) for o in response.outputs]
            structured = None
            if len(outputs) == 1 and outputs[0].get("type") == "json" and isinstance(outputs[0].get("value"), dict):
                structured = outputs[0]["value"]
            return McpToolResult(
                text=render_outputs(outputs),
                structured=structured,
                is_error=response.exit_code != 0,
            )

        return McpTool(
            name=name,
            description=proc.description or f"Run '{' '.join(path)}'",
            input_schema=command_input_schema(proc),
            call=call,
            required_permissions=list(dict.fromkeys(perms)),
        )
