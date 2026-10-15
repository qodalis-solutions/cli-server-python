"""Built-in feature tools: ``server_info``, filesystem and data explorer.

Each reuses the same service as the matching HTTP route (the file storage
provider with its path whitelist, the data-explorer executor with its
limits), so MCP never bypasses their safety checks.  Service errors become
``isError`` results."""

from __future__ import annotations

import codecs
import json
import platform
from dataclasses import asdict
from typing import Any, Callable

from .core import TIER_BUILTIN, McpCallContext, McpTool, McpToolResult, object_schema

READ_ONLY = {"readOnlyHint": True}
DESTRUCTIVE = {"destructiveHint": True}
DEFAULT_MAX_BYTES = 262144

_PATH = {"type": "string", "description": "Path of the file or directory"}


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _guard(fn: Callable[[dict[str, Any], McpCallContext], Any]) -> Callable[..., Any]:
    """Service exceptions become ``isError`` results with the message."""

    async def call(arguments: dict[str, Any], context: McpCallContext) -> McpToolResult:
        try:
            return await fn(arguments, context)
        except Exception as exc:  # noqa: BLE001 - reported to the client
            return McpToolResult.error(str(exc) or type(exc).__name__)

    return call


# ---------------------------------------------------------------------------
# server_info
# ---------------------------------------------------------------------------


class ServerInfoMcpToolProvider:
    """``server_info``: version, capabilities, enabled features and the
    registered command names.  Visible to any caller who reached the
    endpoint."""

    mcp_tier = TIER_BUILTIN
    name = "server"

    def __init__(self, version: str, registry: Any, features: Callable[[], list[str]], shell_enabled: bool) -> None:
        self.version = version
        self.registry = registry
        self.features = features
        self.shell_enabled = shell_enabled

    def get_tools(self, context: McpCallContext) -> list[McpTool]:
        async def call(arguments: dict[str, Any], ctx: McpCallContext) -> McpToolResult:
            system = platform.system()
            info = {
                "name": "qodalis-cli-server",
                "version": self.version,
                "capabilities": {
                    "os": "darwin" if system == "Darwin" else "win32" if system == "Windows" else "linux",
                    "shell": self.shell_enabled,
                    "streaming": True,
                },
                "features": self.features(),
                "commands": sorted(p.command for p in self.registry.processors),
            }
            return McpToolResult(text=_json(info), structured=info)

        return [McpTool(
            name="server_info",
            description="Server version, capabilities, enabled features and registered command names",
            input_schema=object_schema(),
            call=call,
            annotations=dict(READ_ONLY),
        )]


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------


class FilesystemMcpToolProvider:
    """``fs_*`` tools over the server's file storage provider (the one
    behind ``/api/qcli/fs``)."""

    mcp_tier = TIER_BUILTIN
    name = "filesystem"

    def __init__(self, provider: Any) -> None:
        self.provider = provider

    def get_tools(self, context: McpCallContext) -> list[McpTool]:
        p = self.provider

        async def fs_list(args: dict[str, Any], _: McpCallContext) -> McpToolResult:
            entries = [asdict(e) for e in await p.list(args["path"])]
            lines = [
                f"{e['name']}{'/' if e['type'] == 'directory' else ''}"
                + ("" if e["type"] == "directory" else f"  {e['size']} bytes")
                for e in entries
            ]
            return McpToolResult(text="\n".join(lines), structured={"path": args["path"], "entries": entries})

        async def fs_stat(args: dict[str, Any], _: McpCallContext) -> McpToolResult:
            info = asdict(await p.stat(args["path"]))
            return McpToolResult(text="\n".join(f"{k}: {v}" for k, v in info.items()), structured=info)

        async def fs_read(args: dict[str, Any], _: McpCallContext) -> McpToolResult:
            path = args["path"]
            max_bytes = int(args.get("maxBytes", DEFAULT_MAX_BYTES))
            info = await p.stat(path)
            if info.type == "directory":
                return McpToolResult.error(f"Is a directory: {path}")
            data = bytearray()
            stream = await p.get_download_stream(path)
            async for chunk in stream:
                data.extend(chunk)
                if len(data) > max_bytes:
                    break
            total = max(info.size, len(data))
            truncated = len(data) > max_bytes
            head = bytes(data[:max_bytes])
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            try:
                # final=False when truncated: a multi-byte character cut at
                # the limit is dropped instead of failing the decode.
                text = decoder.decode(head, final=not truncated)
            except UnicodeDecodeError:
                return McpToolResult.error(f"binary file, {total} bytes")
            if "\x00" in text:
                return McpToolResult.error(f"binary file, {total} bytes")
            if truncated:
                text += f"\n\n[truncated: showing the first {max_bytes} of {total} bytes]"
            return McpToolResult(text=text)

        async def fs_write(args: dict[str, Any], _: McpCallContext) -> McpToolResult:
            content = args["content"]
            await p.write_file(args["path"], content)
            return McpToolResult(text=f"Wrote {len(content.encode('utf-8'))} bytes to {args['path']}")

        async def fs_mkdir(args: dict[str, Any], _: McpCallContext) -> McpToolResult:
            await p.mkdir(args["path"], recursive=True)
            return McpToolResult(text=f"Created {args['path']}")

        async def fs_delete(args: dict[str, Any], _: McpCallContext) -> McpToolResult:
            await p.remove(args["path"], recursive=bool(args.get("recursive", False)))
            return McpToolResult(text=f"Deleted {args['path']}")

        path_only = object_schema({"path": _PATH}, ["path"])
        return [
            McpTool("fs_list", "List a directory", path_only, _guard(fs_list), ["files:read"], dict(READ_ONLY)),
            McpTool("fs_stat", "File or directory metadata", path_only, _guard(fs_stat), ["files:read"], dict(READ_ONLY)),
            McpTool(
                "fs_read",
                "Read a UTF-8 text file (binary files are refused)",
                object_schema({
                    "path": _PATH,
                    "maxBytes": {
                        "type": "integer", "minimum": 1, "default": DEFAULT_MAX_BYTES,
                        "description": "Maximum number of bytes to return",
                    },
                }, ["path"]),
                _guard(fs_read), ["files:read"], dict(READ_ONLY),
            ),
            McpTool(
                "fs_write",
                "Create or overwrite a text file",
                object_schema({"path": _PATH, "content": {"type": "string", "description": "File content (UTF-8)"}},
                              ["path", "content"]),
                _guard(fs_write), ["files:write"],
            ),
            McpTool("fs_mkdir", "Create a directory (with missing parents)", path_only, _guard(fs_mkdir), ["files:write"]),
            McpTool(
                "fs_delete",
                "Delete a file or directory",
                object_schema({
                    "path": _PATH,
                    "recursive": {"type": "boolean", "default": False,
                                  "description": "Delete a non-empty directory with its content"},
                }, ["path"]),
                _guard(fs_delete), ["files:write"], dict(DESTRUCTIVE),
            ),
        ]


# ---------------------------------------------------------------------------
# Data explorer
# ---------------------------------------------------------------------------


def _camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(w.capitalize() for w in parts[1:])


def _camel_keys(obj: Any, depth: int = 0) -> Any:
    """Same key conversion as the data-explorer HTTP API (row data kept)."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            key = _camel(k) if depth < 2 else k
            out[key] = v if key == "rows" else _camel_keys(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [_camel_keys(item, depth) for item in obj]
    return obj


def _plain(value: Any) -> Any:
    return getattr(value, "value", value)


class DataExplorerMcpToolProvider:
    """``data_*`` tools over the data-explorer registry and executor."""

    mcp_tier = TIER_BUILTIN
    name = "data-explorer"

    def __init__(self, registry: Any, executor: Any) -> None:
        self.registry = registry
        self.executor = executor

    def get_tools(self, context: McpCallContext) -> list[McpTool]:
        registry, executor = self.registry, self.executor

        async def data_sources(args: dict[str, Any], _: McpCallContext) -> McpToolResult:
            sources = json.loads(json.dumps(_camel_keys(registry.get_sources()), default=_plain))
            lines = [f"{s.get('name')}: {s.get('description', '')} ({s.get('language')})" for s in sources]
            return McpToolResult(text="\n".join(lines), structured={"sources": sources})

        async def data_schema(args: dict[str, Any], _: McpCallContext) -> McpToolResult:
            entry = registry.get(args["source"])
            if entry is None:
                return McpToolResult.error(f"Unknown data source: '{args['source']}'")
            provider, options = entry
            schema = await provider.get_schema_async(options)
            if schema is None:
                return McpToolResult.error("Schema introspection is not supported by this data source.")
            data = _camel_keys(asdict(schema))
            lines = []
            for table in data.get("tables", []):
                cols = ", ".join(f"{c['name']} {c['type']}" for c in table.get("columns", []))
                lines.append(f"{table['name']} ({table['type']}): {cols}")
            return McpToolResult(text="\n".join(lines), structured=data)

        async def data_query(args: dict[str, Any], _: McpCallContext) -> McpToolResult:
            result = await executor.execute_async({
                "source": args["source"],
                "query": args["query"],
                "parameters": args.get("parameters"),
            })
            data = json.loads(json.dumps(_camel_keys(asdict(result)), default=_plain))
            if not result.success:
                return McpToolResult(text=result.error or "query failed", structured=data, is_error=True)
            columns = list(result.columns or [])
            rows = result.rows or []
            lines: list[str] = []
            if rows and isinstance(rows[0], dict):
                columns = columns or list(rows[0].keys())
                lines.append(" | ".join(columns))
                lines.extend(" | ".join(str(r.get(c, "")) for c in columns) for r in rows)  # type: ignore[union-attr]
            elif columns or rows:
                if columns:
                    lines.append(" | ".join(columns))
                lines.extend(" | ".join(str(c) for c in r) for r in rows)  # type: ignore[union-attr]
            lines.append(f"({result.row_count} row{'s' if result.row_count != 1 else ''}"
                         f"{', truncated' if result.truncated else ''})")
            return McpToolResult(text="\n".join(lines), structured=data)

        source = {"type": "string", "description": "Data source name (see data_sources)"}
        return [
            McpTool("data_sources", "List the data-explorer sources", object_schema(),
                    _guard(data_sources), ["data:query"], dict(READ_ONLY)),
            McpTool("data_schema", "Tables and columns of a data source",
                    object_schema({"source": source}, ["source"]),
                    _guard(data_schema), ["data:query"], dict(READ_ONLY)),
            McpTool(
                "data_query",
                "Run a query against a data source",
                object_schema({
                    "source": source,
                    "query": {"type": "string", "description": "Query in the source's language"},
                    "parameters": {"type": "object", "description": "Query parameters"},
                }, ["source", "query"]),
                _guard(data_query), ["data:query"],
            ),
        ]
