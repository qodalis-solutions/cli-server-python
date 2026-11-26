"""``notifications``: the caller's system notifications from the terminal.  ``list [--unread] [--severity s]``,
``read <id>|--all``, ``clear <id> [--all]`` (``--all`` clears it for everyone and needs ``notifications:manage``).
Ids may be shortened to a unique prefix, or given as the notification's key."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional, Union

from qodalis_cli_server_abstractions import (
    CLI_NOTIFICATIONS_MANAGE_PERMISSION,
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliCommandRefusal,
    CliProcessCommand,
    ICliCommandProcessor,
    get_positionals,
)

from .cli_notification_service import CliNotificationService, CliNotificationViewer

#: The viewer of the command's caller (the server decides: the signed-in user, or everyone on an open server).
CliNotificationViewerResolver = Callable[[], CliNotificationViewer]

_SEVERITY_MARK = {"info": "i", "warning": "!", "error": "x"}


def _flag(command: CliProcessCommand, name: str) -> bool:
    value = (command.args or {}).get(name)
    return value is True or value == "true"


def format_notification_line(n: dict[str, Any]) -> str:
    """One notification as a line: ``* ! 2026-11-26 10:00  Restart required  (n_ab12…)``."""
    when = str(n.get("updatedAt", "")).replace("T", " ")[:16]
    return f"{' ' if n.get('read') else '*'} {_SEVERITY_MARK.get(n.get('severity', ''), ' ')} {when}  {n['title']}  ({n['id']})"


def _find(service: CliNotificationService, viewer: CliNotificationViewer, ref: str) -> Union[dict[str, Any], str]:
    """A notification by its id, a unique id prefix or its key; the error text otherwise."""
    exact = service.find(viewer, ref)
    if exact is not None:
        return exact
    matches = [n for n in service.list(viewer)["items"] if n["id"].startswith(ref) or n["key"] == ref]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return f"Error: {ref} matches {len(matches)} notifications; use the full id"
    return f"Error: notification not found: {ref}"


class _NotificationsSubcommand(CliCommandProcessor):
    def __init__(self, service: CliNotificationService, viewer: CliNotificationViewerResolver) -> None:
        self.service = service
        self.viewer = viewer


class NotificationsListProcessor(_NotificationsSubcommand):
    command = "list"
    description = "Lists your notifications, newest first (* unread)"
    parameters = [
        CliCommandParameterDescriptor("unread", "Only the unread ones", False, "boolean"),
        CliCommandParameterDescriptor("severity", "Only info, warning or error", False, "string"),
    ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        unread = _flag(command, "unread")
        severity = (command.args or {}).get("severity")
        result = self.service.list(self.viewer(), unread=unread, severity=severity if isinstance(severity, str) else None)
        items = result["items"]
        if not items:
            return "No unread notifications." if unread else "No notifications."
        lines = [f"{len(items)} notification(s), {result['unreadCount']} unread"]
        for n in items:
            lines.append(format_notification_line(n))
            if n.get("message"):
                lines.append("      " + n["message"].replace("\n", "\n      "))
        return "\n".join(lines)


class NotificationsReadProcessor(_NotificationsSubcommand):
    command = "read"
    description = "Marks a notification read (notifications read <id>), or all of them (--all)"
    parameters = [CliCommandParameterDescriptor("all", "Mark every notification read", False, "boolean")]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        viewer = self.viewer()
        if _flag(command, "all"):
            return f"Marked {self.service.mark_all_read(viewer)} notification(s) read."
        positionals = get_positionals(command)
        if not positionals:
            return "Error: usage: notifications read <id> | notifications read --all"
        found = _find(self.service, viewer, positionals[0])
        if isinstance(found, str):
            return found
        self.service.mark_read(viewer, found["id"])
        return f"Read: {found['title']}"


class NotificationsClearProcessor(_NotificationsSubcommand):
    command = "clear"
    description = (
        f"Dismisses a notification for you (notifications clear <id>); --all clears it for everyone "
        f"({CLI_NOTIFICATIONS_MANAGE_PERMISSION})"
    )
    value_required: Optional[bool] = True
    parameters = [
        CliCommandParameterDescriptor("all", f"Clear it for every user ({CLI_NOTIFICATIONS_MANAGE_PERMISSION})", False, "boolean"),
    ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        viewer = self.viewer()
        positionals = get_positionals(command)
        if not positionals:
            return "Error: usage: notifications clear <id> [--all]"
        everyone = _flag(command, "all")
        if everyone and not viewer.can(CLI_NOTIFICATIONS_MANAGE_PERMISSION):
            return CliCommandRefusal(f"Error: forbidden: missing permission {CLI_NOTIFICATIONS_MANAGE_PERMISSION}")
        found = _find(self.service, viewer, positionals[0])
        if isinstance(found, str):
            return found
        if everyone:
            self.service.clear(viewer, found["id"])
            return f"Cleared for everyone: {found['title']}"
        self.service.dismiss(viewer, found["id"])
        return f"Dismissed: {found['title']}"


class CliNotificationsCommandProcessor(CliCommandProcessor):
    """``notifications``: the caller's system notifications (see the module's docstring); no permission needed, each
    caller sees their own."""

    command = "notifications"
    description = "System notifications: updates, restarts, plugin problems, security warnings"
    module = "notifications"
    icon = "🔔"

    def __init__(self, service: CliNotificationService, viewer: CliNotificationViewerResolver) -> None:
        self._list = NotificationsListProcessor(service, viewer)
        self._processors: list[ICliCommandProcessor] = [
            self._list, NotificationsReadProcessor(service, viewer), NotificationsClearProcessor(service, viewer),
        ]

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return self._processors

    @property
    def parameters(self) -> list[Any]:
        return self._list.parameters

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return await self._list.handle_async(command, cancellation_event)
