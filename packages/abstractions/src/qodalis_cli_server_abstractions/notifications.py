"""System notifications: what the server tells its users and admins (an update is available, a restart is required,
a plugin failed to load or needs configuration, a security warning).  The same contract and JSON as the Node and .NET
servers (``docs/notifications-spec.md`` of ``@qodalis/cli-server-node``).

A source publishes with ``notify({"key": ..., ...})``: the key names the condition, so publishing the same key again
updates the notification instead of adding one, and ``resolve(key)`` removes it once the condition ended (the update
was applied, the server restarted).  Plugins publish through ``context.notifications`` (their entry) or
``builder.notifications`` (code)."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Literal, Mapping, Optional, Protocol, runtime_checkable

#: How serious a notification is.
CliNotificationSeverity = Literal["info", "warning", "error"]
#: The severities, mildest first.
CLI_NOTIFICATION_SEVERITIES: tuple[str, ...] = ("info", "warning", "error")

#: What ``notification:changed`` says happened.
CliNotificationChange = Literal["created", "updated", "resolved", "read", "read-all", "dismissed", "cleared"]

#: Published on the event bus (and the events socket) when a notification changes.
CLI_NOTIFICATION_CHANGED_EVENT = "notification:changed"
#: Clearing a notification for everyone (``DELETE /api/v1/qcli/notifications/{id}?scope=all``,
#: ``notifications clear --all``).
CLI_NOTIFICATIONS_MANAGE_PERMISSION = "notifications:manage"
#: Seeing the available updates and checking for them (``/api/v1/qcli/updates``).
CLI_UPDATES_READ_PERMISSION = "updates:read"


class CliNotificationKeys:
    """The keys of the server's own sources (the same on every platform)."""

    #: Changes wait for a restart.
    RESTART_REQUIRED = "server.restart-required"
    #: ``allow_anonymous_admin`` is on.
    ANONYMOUS_ADMIN = "security.anonymous-admin"
    #: ``plugin.failed:<plugin name>``: a plugin failed to load at the last start.
    PLUGIN_FAILED_PREFIX = "plugin.failed:"
    #: ``plugin.needs-configuration:<plugin name>``: required settings without a value.
    PLUGIN_NEEDS_CONFIGURATION_PREFIX = "plugin.needs-configuration:"
    #: ``update:dashboard:<name>``, ``update:server:<name>``, ``update:code:<kind>:<package>``,
    #: ``update:core:<package>``.
    UPDATE_PREFIX = "update:"


_KEY = re.compile(r"^[A-Za-z0-9._:@/+-]{1,200}\Z")
_KIND = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}\Z")
_PERMISSION = re.compile(r"^[A-Za-z0-9_.*-]+:[A-Za-z0-9_.*-]+\Z")
_LINK_PATH = re.compile(r"^/(?!/)")
_LINK_URL = re.compile(r"^https?://", re.IGNORECASE)
#: The most JSON ``data`` may take.
MAX_NOTIFICATION_DATA_BYTES = 8192


def is_cli_notification_key(key: Any) -> bool:
    """True for a valid notification key."""
    return isinstance(key, str) and bool(_KEY.match(key))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _get(source: Any, *names: str) -> Any:
    for name in names:
        if isinstance(source, Mapping):
            if name in source:
                return source[name]
        elif hasattr(source, name):
            return getattr(source, name)
    return None


def normalize_cli_notification_input(value: Any) -> dict[str, Any]:
    """Checks and normalizes what a source publishes (a dict with ``key``, ``kind``, ``severity``, ``title``,
    ``message``, and optionally ``link``, ``requiredPermission`` (or ``required_permission``) and ``data``; or an
    object with those attributes): trimmed texts, optional fields left out when unset.  Raises ``ValueError`` naming
    the field for an invalid one."""
    key = _get(value, "key")
    if not is_cli_notification_key(key):
        raise ValueError("notification key must be 1-200 characters of [A-Za-z0-9._:@/+-]")
    kind = _text(_get(value, "kind"))
    if not _KIND.match(kind):
        raise ValueError("notification kind must be 1-40 characters of [a-z0-9._-]")
    severity = _get(value, "severity")
    if severity not in CLI_NOTIFICATION_SEVERITIES:
        raise ValueError("notification severity must be info, warning or error")
    title = _text(_get(value, "title"))
    if not title or len(title) > 200:
        raise ValueError("notification title must be 1-200 characters")
    message = _text(_get(value, "message"))
    if len(message) > 2000:
        raise ValueError("notification message must be at most 2000 characters")
    out: dict[str, Any] = {"key": key, "kind": kind, "severity": severity, "title": title, "message": message}
    link = _get(value, "link")
    if link is not None and link != "":
        if not isinstance(link, str) or len(link) > 2000 or not (_LINK_PATH.match(link) or _LINK_URL.match(link)):
            raise ValueError("notification link must be a path starting with / or an http(s) URL")
        out["link"] = link
    permission = _get(value, "requiredPermission", "required_permission")
    if permission is not None and permission != "":
        if not isinstance(permission, str) or not _PERMISSION.match(permission):
            raise ValueError("notification requiredPermission must be resource:action")
        out["requiredPermission"] = permission
    data = _get(value, "data")
    if data is not None:
        if not isinstance(data, Mapping):
            raise ValueError("notification data must be an object")
        try:
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("notification data must be JSON") from exc
        if len(text.encode("utf-8")) > MAX_NOTIFICATION_DATA_BYTES:
            raise ValueError("notification data must be at most 8 KB of JSON")
        out["data"] = json.loads(text)
    return out


@runtime_checkable
class ICliNotifications(Protocol):
    """The server's notifications (``builder.notifications``, ``context.notifications``).

    Notifications are plain dicts in their JSON shape (``id``, ``key``, ``kind``, ``severity``, ``title``,
    ``message``, ``link``, ``requiredPermission``, ``data``, ``createdAt``, ``updatedAt``)."""

    def notify(self, notification: Any) -> dict[str, Any]:
        """Adds the notification, or updates the one with the same key (unread again when its content changed)."""
        ...

    def resolve(self, key: str) -> bool:
        """Removes the notification of a key whose condition ended; true when there was one."""
        ...

    def resolve_missing(self, prefix: str, keep: Iterable[str]) -> int:
        """Removes the notifications whose key starts with ``prefix`` and is not in ``keep``: a source that
        re-evaluates its conditions (at startup, after a check) resolves what it no longer sees.  Returns how many."""
        ...

    def get(self, key: str) -> Optional[dict[str, Any]]:
        """The notification of a key (no per-user state), ``None`` when none."""
        ...


#: Where an update comes from: ``marketplace`` (update it here), ``code`` (added in the host's code), or the
#: server package's registry (``pypi`` on Python, ``npm`` on Node, ``nuget`` on .NET).
CliUpdateSource = str


__all__ = [
    "CLI_NOTIFICATIONS_MANAGE_PERMISSION",
    "CLI_NOTIFICATION_CHANGED_EVENT",
    "CLI_NOTIFICATION_SEVERITIES",
    "CLI_UPDATES_READ_PERMISSION",
    "CliNotificationChange",
    "CliNotificationKeys",
    "CliNotificationSeverity",
    "CliUpdateSource",
    "ICliNotifications",
    "MAX_NOTIFICATION_DATA_BYTES",
    "is_cli_notification_key",
    "normalize_cli_notification_input",
]
