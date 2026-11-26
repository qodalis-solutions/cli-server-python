"""The server's own notification sources: restart required, the anonymous-admin security warning, and plugins that
failed to load or need configuration."""

from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import quote, urlencode

from qodalis_cli_server_abstractions import (
    CLI_SERVER_PLUGIN_FAILED,
    CLI_SERVER_PLUGIN_NEEDS_CONFIGURATION,
    CliNotificationKeys,
)

#: The permission the server's own notifications need (the dashboard's audience).
SYSTEM_NOTIFICATION_PERMISSION = "admin:access"


def sync_restart_notification(notifications: Any, pending: Iterable[Any]) -> None:
    """"Restart required" while changes wait for a restart; resolved once none do (after every start)."""
    items = list(pending)
    if not items:
        notifications.resolve(CliNotificationKeys.RESTART_REQUIRED)
        return
    count = len(items)
    notifications.notify({
        "key": CliNotificationKeys.RESTART_REQUIRED,
        "kind": "restart",
        "severity": "warning",
        "title": "Restart required: 1 change waits for a restart" if count == 1
        else f"Restart required: {count} changes wait for a restart",
        "message": "\n".join(getattr(p, "description", None) or getattr(p, "id", "") for p in items)[:2000],
        "link": "/",
        "requiredPermission": SYSTEM_NOTIFICATION_PERMISSION,
        "data": {"pending": [getattr(p, "id", None) for p in items]},
    })


def sync_anonymous_admin_notification(notifications: Any, on: bool) -> None:
    """The security warning while ``allow_anonymous_admin`` is on; resolved when it is off."""
    if not on:
        notifications.resolve(CliNotificationKeys.ANONYMOUS_ADMIN)
        return
    notifications.notify({
        "key": CliNotificationKeys.ANONYMOUS_ADMIN,
        "kind": "security",
        "severity": "warning",
        "title": "Anyone can administer this server",
        "message": (
            "allow_anonymous_admin is on and no sign-in protects the server: anyone who can reach it can install "
            "plugins (code that runs in the server), change its configuration and restart it. Protect it with the "
            "users plugin, or turn allow_anonymous_admin off."
        ),
        "link": "/config",
        "requiredPermission": SYSTEM_NOTIFICATION_PERMISSION,
    })


def sync_plugin_notifications(notifications: Any, states: Iterable[Any]) -> None:
    """"Plugin failed to load" and "Plugin needs configuration" from what this start loaded; the notifications of
    plugins that load now (or are gone) are resolved."""
    failed: list[str] = []
    needs: list[str] = []
    for s in states:
        plugin_path = f"/plugins/{quote(s.package, safe='')}"
        if s.status == CLI_SERVER_PLUGIN_FAILED:
            key = f"{CliNotificationKeys.PLUGIN_FAILED_PREFIX}{s.name}"
            failed.append(key)
            notifications.notify({
                "key": key,
                "kind": "plugin",
                "severity": "error",
                "title": f"Plugin {s.name} failed to load",
                "message": f"{s.package}@{s.version}: {s.error or 'unknown error'}"[:2000],
                "link": plugin_path,
                "requiredPermission": SYSTEM_NOTIFICATION_PERMISSION,
                "data": {
                    "plugin": s.name, "package": s.package, "version": s.version, "source": s.source,
                    "error": s.error or None,
                },
            })
        elif s.status == CLI_SERVER_PLUGIN_NEEDS_CONFIGURATION:
            key = f"{CliNotificationKeys.PLUGIN_NEEDS_CONFIGURATION_PREFIX}{s.name}"
            needs.append(key)
            missing = list(s.missing or [])
            notifications.notify({
                "key": key,
                "kind": "plugin",
                "severity": "warning",
                "title": f"Plugin {s.name} needs configuration",
                "message": f"Set {', '.join(missing) if missing else 'its required settings'}, then restart the server.",
                "link": f"/config?{urlencode({'provider': s.settings or s.name})}",
                "requiredPermission": SYSTEM_NOTIFICATION_PERMISSION,
                "data": {
                    "plugin": s.name, "package": s.package, "version": s.version, "source": s.source, "missing": missing,
                },
            })
    notifications.resolve_missing(CliNotificationKeys.PLUGIN_FAILED_PREFIX, failed)
    notifications.resolve_missing(CliNotificationKeys.PLUGIN_NEEDS_CONFIGURATION_PREFIX, needs)
