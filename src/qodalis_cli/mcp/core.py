"""MCP tool model, provider registry and the small JSON-schema validator
used for ``tools/call`` arguments."""

from __future__ import annotations

import inspect
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, Union, runtime_checkable

logger = logging.getLogger(__name__)

TOOL_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

#: Provider tiers — on a name collision the lower tier wins: built-in
#: feature tools, then command tools, then plugin/host providers in
#: registration order.
TIER_BUILTIN = 0
TIER_COMMANDS = 1
TIER_PLUGIN = 2


@dataclass
class CliMcpOptions:
    """Options of the MCP endpoint (``CliServerOptions.mcp``)."""

    #: ``True`` by default when an options object is given.
    enabled: bool = True
    #: Allow-list of tool names; a trailing ``*`` matches a prefix
    #: (``"weather_*"``).  ``None`` = every tool.
    tools: list[str] | None = None
    #: Deny-list (same syntax), applied after ``tools``.
    exclude: list[str] | None = None
    #: Only without an auth provider: required static bearer token.
    token: str | None = None
    #: Returned by ``initialize``.
    instructions: str | None = None


def resolve_mcp_options(value: Any) -> CliMcpOptions | None:
    """Normalise ``CliServerOptions.mcp`` (``bool``, ``dict`` or
    :class:`CliMcpOptions`) — ``None`` when the endpoint is disabled."""
    if value is None or value is False:
        return None
    if value is True:
        return CliMcpOptions()
    if isinstance(value, dict):
        value = CliMcpOptions(**value)
    if not isinstance(value, CliMcpOptions):
        raise TypeError("mcp must be a bool, a dict or CliMcpOptions")
    return value if value.enabled else None


@dataclass
class McpToolResult:
    """What a tool returns: plain ``text``, optional ``structured`` content
    (an object) and whether it failed."""

    text: str
    structured: dict[str, Any] | None = None
    is_error: bool = False

    @staticmethod
    def error(message: str) -> "McpToolResult":
        return McpToolResult(text=message, is_error=True)


@dataclass
class McpCallContext:
    """The caller of an MCP request.

    ``auth`` is the server auth context (``None`` without an auth provider);
    ``request`` the HTTP request."""

    auth: Any = None
    request: Any = None

    def can(self, permission: str) -> bool:
        return self.auth is None or bool(self.auth.can(permission))


ToolHandler = Callable[[dict[str, Any], McpCallContext], Awaitable[McpToolResult]]


@dataclass
class McpTool:
    """A tool definition contributed by an :class:`IMcpToolProvider`."""

    #: ``[a-z0-9_-]``, at most 64 characters.
    name: str
    description: str
    #: JSON schema: ``{type: "object", properties, required, additionalProperties: false}``.
    input_schema: dict[str, Any]
    #: ``call(arguments, context)`` — arguments are validated first.
    call: ToolHandler
    #: Checked for ``tools/list`` and ``tools/call`` (only with an auth provider).
    required_permissions: list[str] = field(default_factory=list)
    #: MCP tool annotations (``readOnlyHint``, ``destructiveHint``, ``idempotentHint``).
    annotations: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.annotations:
            result["annotations"] = dict(self.annotations)
        return result


ToolsResult = Union[list[McpTool], Awaitable[list[McpTool]]]


@runtime_checkable
class IMcpToolProvider(Protocol):
    """Contributes MCP tools.  ``get_tools`` is called on every request (may
    be sync or async) so tools always reflect the current state."""

    def get_tools(self, context: McpCallContext) -> ToolsResult: ...


def object_schema(
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    """``{type: "object", properties, required, additionalProperties: false}``."""
    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required or []),
        "additionalProperties": False,
    }


def _matches(name: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        p = pattern.strip().lower()
        if p.endswith("*"):
            if name.startswith(p[:-1]):
                return True
        elif name == p:
            return True
    return False


class McpToolRegistry:
    """Tool providers of a CLI server (``CliServerResult.mcp``).

    Plugins and host apps add providers with :meth:`add_provider`; it is
    available whether or not the endpoint is enabled."""

    def __init__(self, options: CliMcpOptions | None = None) -> None:
        self.options = options
        self._providers: list[tuple[int, int, IMcpToolProvider]] = []

    @property
    def enabled(self) -> bool:
        return self.options is not None

    def add_provider(self, provider: IMcpToolProvider, *, builtin: bool = False) -> "McpToolRegistry":
        """Register a tool provider.  ``builtin`` marks a server feature
        (its tools win name collisions); other providers come after the
        command tools, in registration order."""
        tier = getattr(provider, "mcp_tier", None)
        if tier is None:
            tier = TIER_BUILTIN if builtin else TIER_PLUGIN
        self._providers.append((tier, len(self._providers), provider))
        return self

    @property
    def providers(self) -> list[IMcpToolProvider]:
        return [p for _, _, p in sorted(self._providers, key=lambda x: (x[0], x[1]))]

    async def tools(self, context: McpCallContext) -> dict[str, McpTool]:
        """Tools visible to ``context``, by name (first registered wins)."""
        options = self.options or CliMcpOptions()
        out: dict[str, McpTool] = {}
        for provider in self.providers:
            try:
                result = provider.get_tools(context)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:
                logger.exception("MCP tool provider %s failed", type(provider).__name__)
                continue
            for tool in result or []:
                name = tool.name
                if not TOOL_NAME_RE.match(name or ""):
                    logger.warning("MCP tool with an invalid name ignored: %r", name)
                    continue
                if options.tools is not None and not _matches(name, options.tools):
                    continue
                if options.exclude and _matches(name, options.exclude):
                    continue
                if not all(context.can(p) for p in tool.required_permissions):
                    continue
                if name in out:
                    logger.warning("MCP tool name collision: '%s' from %s ignored", name, type(provider).__name__)
                    continue
                out[name] = tool
        return out


# ---------------------------------------------------------------------------
# Argument validation (the JSON-schema subset the tools use)
# ---------------------------------------------------------------------------


def _type_ok(value: Any, expected: Any) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return (
            isinstance(value, int) and not isinstance(value, bool)
        ) or (isinstance(value, float) and value.is_integer())
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


_TYPE_LABELS = {
    "string": "a string",
    "number": "a number",
    "integer": "an integer",
    "boolean": "a boolean",
    "object": "an object",
    "array": "an array",
}


def _check_property(key: str, prop: dict[str, Any], value: Any) -> str | None:
    expected = prop.get("type")
    items_type = (prop.get("items") or {}).get("type") if isinstance(prop.get("items"), dict) else None
    if not _type_ok(value, expected):
        if expected == "array" and items_type:
            return f"argument '{key}' must be an array of {items_type}s"
        return f"argument '{key}' must be {_TYPE_LABELS.get(expected, expected)}"
    if expected == "array" and items_type and any(not _type_ok(v, items_type) for v in value):
        return f"argument '{key}' must be an array of {items_type}s"
    return None


def validate_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """Validate tool ``arguments`` against its input schema (declared types,
    array item types, unknown and missing required arguments; ``null``
    counts as absent).  Returns the reason when invalid."""
    properties: dict[str, Any] = schema.get("properties") or {}
    for key, value in arguments.items():
        prop = properties.get(key)
        if prop is None:
            return f"unknown argument '{key}'"
        if value is None:
            continue
        reason = _check_property(key, prop, value)
        if reason:
            return reason
    for key in schema.get("required") or []:
        if arguments.get(key) is None:
            return f"missing required argument '{key}'"
    return None
