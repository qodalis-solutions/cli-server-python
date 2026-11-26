"""``updates_list``: the update checker's last result as an MCP tool (``check: true`` checks first)."""

from __future__ import annotations

import json
from typing import Any

from ..controllers.updates_controller import can_read_updates
from .update_checker import UpdateChecker


class UpdatesMcpToolProvider:
    """``updates_list`` ``{ check? }``: ``updates:read`` or ``plugins:install`` on a protected server."""

    name = "updates"

    def __init__(self, checker: UpdateChecker) -> None:
        self.checker = checker

    def get_tools(self, context: Any) -> list[Any]:
        from qodalis_cli.mcp import McpTool, McpToolResult, object_schema

        checker = self.checker

        async def updates_list(args: dict[str, Any], ctx: Any) -> Any:
            auth = getattr(ctx, "auth", None)
            if auth is not None and not can_read_updates(list(getattr(auth, "permissions", None) or [])):
                return McpToolResult.error("forbidden: missing permission updates:read")
            result = await checker.check() if args.get("check") is True else checker.result()
            server = result.get("server")
            items = [
                *result["dashboardPlugins"], *result["serverPlugins"], *result["codePlugins"],
                *([server] if isinstance(server, dict) and server.get("updateAvailable") else []),
            ]
            lines = [
                f"{i['kind']} {i['package']}: {i.get('current') or '?'} -> {i.get('latest') or '?'} "
                f"({'update from the dashboard' if i.get('canUpdate') else i.get('reason') or 'update in code'})"
                for i in items
            ]
            error = f" ({result['error']})" if result.get("error") else ""
            text = f"{result['count']} update(s) available; last checked {result.get('checkedAt') or 'never'}{error}"
            if lines:
                text += "\n" + "\n".join(lines)
            return McpToolResult(text=text, structured=json.loads(json.dumps(result)))

        return [
            McpTool(
                name="updates_list",
                description=(
                    "List the available updates: dashboard and server plugins installed from the marketplace with a "
                    "newer version, plugins added in code with a newer package, and the server package; check: true "
                    "asks the marketplace first"
                ),
                input_schema=object_schema({
                    "check": {"type": "boolean", "description": "Check now instead of answering the last result"},
                }),
                call=updates_list,
                annotations={"readOnlyHint": True},
            ),
        ]
