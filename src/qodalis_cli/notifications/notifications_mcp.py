"""``notifications_list``: the caller's notifications as an MCP tool (read-only; marking them read stays a user's
action)."""

from __future__ import annotations

import json
from typing import Any, Callable

from ..mcp.core import TIER_BUILTIN, McpCallContext, McpTool, McpToolResult, object_schema
from .cli_notification_service import CliNotificationService, CliNotificationViewer


class NotificationsMcpToolProvider:
    """``notifications_list`` ``{ unread?, severity?, limit? (default 50) }``: ``{ items, unreadCount }``."""

    mcp_tier = TIER_BUILTIN
    name = "notifications"

    def __init__(self, service: CliNotificationService, viewer_of: Callable[[Any], CliNotificationViewer]) -> None:
        self.service = service
        self.viewer_of = viewer_of

    def get_tools(self, context: McpCallContext) -> list[McpTool]:
        service = self.service

        async def notifications_list(args: dict[str, Any], ctx: McpCallContext) -> McpToolResult:
            limit = args.get("limit")
            result = service.list(
                self.viewer_of(ctx.auth),
                unread=args.get("unread") is True,
                severity=args["severity"] if isinstance(args.get("severity"), str) else None,
                limit=int(limit) if isinstance(limit, (int, float)) and not isinstance(limit, bool) else 50,
            )
            lines = [
                f"{' ' if n['read'] else '*'} [{n['severity']}] {n['title']}{': ' + n['message'] if n['message'] else ''}"
                for n in result["items"]
            ]
            text = f"{result['unreadCount']} unread\n" + ("\n".join(lines) if lines else "No notifications.")
            return McpToolResult(text=text, structured=json.loads(json.dumps(result)))

        return [
            McpTool(
                name="notifications_list",
                description=(
                    "List the server's system notifications the caller may see (available updates, restart required, "
                    "plugins that failed to load or need configuration, security warnings), newest first, with the "
                    "unread count"
                ),
                input_schema=object_schema({
                    "unread": {"type": "boolean", "description": "Only the unread ones"},
                    "severity": {"type": "string", "enum": ["info", "warning", "error"], "description": "Only this severity"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "At most this many (default 50)"},
                }),
                call=notifications_list,
                annotations={"readOnlyHint": True},
            ),
        ]
