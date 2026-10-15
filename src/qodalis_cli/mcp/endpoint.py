"""``/api/qcli/mcp`` — MCP "Streamable HTTP" in its stateless, JSON-only
form: each POST carries one JSON-RPC 2.0 message answered with
``application/json`` (no ``Mcp-Session-Id``, no SSE).  Implements the subset
needed for tools: ``initialize``, ``ping``, ``tools/list``, ``tools/call``
and notifications."""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..security import get_auth_context, origin_allowed
from .core import CliMcpOptions, McpCallContext, McpToolRegistry, validate_arguments

logger = logging.getLogger(__name__)

MCP_PATH = "/api/qcli/mcp"
#: Newest first.  ``initialize`` echoes the client's version when supported,
#: otherwise answers with the newest one.
SUPPORTED_PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"]
SERVER_NAME = "qodalis-cli-server"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _rpc_error(id_: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def create_mcp_router(
    tools: McpToolRegistry,
    options: CliMcpOptions,
    *,
    server_version: str,
    allowed_origins: Callable[[], list[str] | None],
    trust_proxy: Callable[[], bool | int] = lambda: False,
) -> APIRouter:
    """Router serving ``/api/qcli/mcp`` (include without a prefix).

    With an auth provider the server auth gate has already authenticated the
    request (and required ``commands:execute``); without one,
    ``options.token`` is a required static bearer token when set."""
    router = APIRouter()

    def method_not_allowed() -> JSONResponse:
        return JSONResponse({"error": "method not allowed"}, status_code=405, headers={"Allow": "POST"})

    @router.get(MCP_PATH, include_in_schema=False)
    async def mcp_get() -> JSONResponse:
        return method_not_allowed()

    @router.delete(MCP_PATH, include_in_schema=False)
    async def mcp_delete() -> JSONResponse:
        return method_not_allowed()

    def initialize(params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": server_version},
        }
        if options.instructions:
            result["instructions"] = options.instructions
        return result

    async def call_tool(params: dict[str, Any], context: McpCallContext) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _RpcError(INVALID_PARAMS, "Invalid params: 'name' is required")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS, "Invalid params: 'arguments' must be an object")
        tool = (await tools.tools(context)).get(name)
        if tool is None:
            raise _RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        error = validate_arguments(arguments, tool.input_schema)
        if error:
            raise _RpcError(INVALID_PARAMS, f"Invalid arguments for tool {name}: {error}")
        try:
            result = await tool.call(arguments, context)
        except Exception as exc:  # noqa: BLE001 - reported as a tool error
            logger.error("MCP tool %s failed: %s", name, exc, exc_info=True)
            return {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True}
        body: dict[str, Any] = {
            "content": [{"type": "text", "text": result.text}],
            "isError": bool(result.is_error),
        }
        if isinstance(result.structured, dict):
            body["structuredContent"] = result.structured
        return body

    async def dispatch(method: str, params: dict[str, Any], context: McpCallContext) -> dict[str, Any]:
        if method == "initialize":
            return initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [t.to_dict() for t in (await tools.tools(context)).values()]}
        if method == "tools/call":
            return await call_tool(params, context)
        raise _RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    @router.post(MCP_PATH, include_in_schema=False)
    async def mcp_post(request: Request) -> Response:
        # DNS-rebinding protection: a browser Origin must be allowed or ours.
        origin = request.headers.get("origin")
        if origin is not None and not origin_allowed(
            origin, allowed_origins() or [], request.scope, trust_proxy()
        ):
            return JSONResponse({"error": "origin not allowed"}, status_code=403)

        auth = get_auth_context(request)
        if auth is None and options.token:
            header = request.headers.get("authorization") or ""
            supplied = header[7:].strip() if header[:7].lower() == "bearer " else ""
            if not hmac.compare_digest(supplied.encode("utf-8"), options.token.encode("utf-8")):
                return JSONResponse(
                    {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"},
                )

        try:
            message = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            return _rpc_error(None, PARSE_ERROR, "Parse error")

        if not isinstance(message, dict):
            return _rpc_error(None, INVALID_REQUEST, "Invalid Request")
        if "method" not in message and ("result" in message or "error" in message):
            return Response(status_code=202)  # a response from the client
        method = message.get("method")
        has_id = "id" in message
        id_ = message.get("id")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str) or (has_id and not _valid_id(id_)):
            return _rpc_error(id_ if _valid_id(id_) else None, INVALID_REQUEST, "Invalid Request")
        if not has_id:
            return Response(status_code=202)  # notification

        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _rpc_error(id_, INVALID_PARAMS, "Invalid params: 'params' must be an object")
        try:
            result = await dispatch(method, params, McpCallContext(auth=auth, request=request))
        except _RpcError as exc:
            return _rpc_error(id_, exc.code, exc.message)
        return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result})

    return router
