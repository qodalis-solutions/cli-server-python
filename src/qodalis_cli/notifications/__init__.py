"""System notifications (``builder.notifications``, ``/api/v1/qcli/notifications``, the ``notifications`` command,
the ``notifications_list`` MCP tool) and the server's own sources."""

from .cli_notification_service import (
    ANONYMOUS_NOTIFICATION_READER,
    ANONYMOUS_NOTIFICATION_VIEWER,
    DEFAULT_NOTIFICATIONS_MAX_AGE_DAYS,
    DEFAULT_NOTIFICATIONS_MAX_ITEMS,
    NOTIFICATIONS_FILE,
    CliNotificationService,
    CliNotificationsOptions,
    CliNotificationViewer,
)
from .notifications_command import CliNotificationsCommandProcessor, format_notification_line
from .notifications_mcp import NotificationsMcpToolProvider
from .notifications_router import create_notifications_router
from .system_notifications import (
    SYSTEM_NOTIFICATION_PERMISSION,
    sync_anonymous_admin_notification,
    sync_plugin_notifications,
    sync_restart_notification,
)

__all__ = [
    "ANONYMOUS_NOTIFICATION_READER",
    "ANONYMOUS_NOTIFICATION_VIEWER",
    "DEFAULT_NOTIFICATIONS_MAX_AGE_DAYS",
    "DEFAULT_NOTIFICATIONS_MAX_ITEMS",
    "NOTIFICATIONS_FILE",
    "SYSTEM_NOTIFICATION_PERMISSION",
    "CliNotificationService",
    "CliNotificationViewer",
    "CliNotificationsCommandProcessor",
    "CliNotificationsOptions",
    "NotificationsMcpToolProvider",
    "create_notifications_router",
    "format_notification_line",
    "sync_anonymous_admin_notification",
    "sync_plugin_notifications",
    "sync_restart_notification",
]
