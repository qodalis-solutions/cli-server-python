"""MCP endpoint — the whole server (commands, filesystem, data explorer,
jobs and plugin tools) as MCP tools at ``POST /api/qcli/mcp``."""

from .commands import CommandsMcpToolProvider, command_tool_name, render_outputs
from .core import (
    CliMcpOptions,
    IMcpToolProvider,
    McpCallContext,
    McpTool,
    McpToolRegistry,
    McpToolResult,
    object_schema,
    resolve_mcp_options,
    validate_arguments,
)
from .endpoint import MCP_PATH, SUPPORTED_PROTOCOL_VERSIONS, create_mcp_router
from .features import (
    DataExplorerMcpToolProvider,
    FilesystemMcpToolProvider,
    ServerInfoMcpToolProvider,
)

__all__ = [
    "CliMcpOptions",
    "CommandsMcpToolProvider",
    "DataExplorerMcpToolProvider",
    "FilesystemMcpToolProvider",
    "IMcpToolProvider",
    "MCP_PATH",
    "McpCallContext",
    "McpTool",
    "McpToolRegistry",
    "McpToolResult",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "ServerInfoMcpToolProvider",
    "command_tool_name",
    "create_mcp_router",
    "object_schema",
    "render_outputs",
    "resolve_mcp_options",
    "validate_arguments",
]
