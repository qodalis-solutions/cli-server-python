"""The server's system notifications (``builder.notifications``): sources publish with :meth:`notify` (a key updates
its notification) and :meth:`resolve` once the condition ended; users list, read and dismiss theirs.  Kept in
``<data_dir>/notifications.json`` (at most 500, 30 days after their last change), with each user's read and dismissed
state; every change is published as ``notification:changed`` (audience: the notification's permission, or the user
for read and dismiss)."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from qodalis_cli_server_abstractions import (
    CLI_NOTIFICATION_CHANGED_EVENT,
    CliEventAudience,
    normalize_cli_notification_input,
)

logger = logging.getLogger("qodalis_cli.notifications")

#: The file below the data directory.
NOTIFICATIONS_FILE = "notifications.json"
#: The most notifications kept (the oldest dropped first).
DEFAULT_NOTIFICATIONS_MAX_ITEMS = 500
#: How long a notification is kept after its last change.
DEFAULT_NOTIFICATIONS_MAX_AGE_DAYS = 30
#: The reader id of every caller on a server without sign-in (one shared read state).
ANONYMOUS_NOTIFICATION_READER = "anonymous"

_CONTENT = ("kind", "severity", "title", "message", "link", "requiredPermission", "data")
_OPTIONAL = ("link", "requiredPermission", "data")


@dataclass(frozen=True)
class CliNotificationViewer:
    """Who reads: their id (read state) and what they may see."""

    user_id: str
    #: True when the viewer holds the permission (always on a server without sign-in).
    can: Callable[[str], bool]


#: The viewer of a server without sign-in: one reader who sees everything.
ANONYMOUS_NOTIFICATION_VIEWER = CliNotificationViewer(ANONYMOUS_NOTIFICATION_READER, lambda _permission: True)


@dataclass
class CliNotificationsOptions:
    """The ``notifications`` option of ``create_cli_server``."""

    #: The file they are kept in (default ``<data_dir>/notifications.json``); ``False`` keeps them in memory.
    file: str | bool | None = None
    #: The most notifications kept (default 500).
    max_items: Optional[int] = None
    #: Days a notification is kept after its last change (default 30).
    max_age_days: Optional[float] = None

    @staticmethod
    def of(value: Any) -> "CliNotificationsOptions":
        if value is None:
            return CliNotificationsOptions()
        if isinstance(value, CliNotificationsOptions):
            return value
        if isinstance(value, dict):
            snake = {"maxItems": "max_items", "maxAgeDays": "max_age_days"}
            return CliNotificationsOptions(**{snake.get(k, k): v for k, v in value.items()})
        raise TypeError("notifications must be a CliNotificationsOptions or a dict")


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(text: str) -> Optional[datetime]:
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _content_of(n: dict[str, Any]) -> str:
    return json.dumps([n.get(k) for k in _CONTENT], sort_keys=True, ensure_ascii=False)


class CliNotificationService:
    """The server's notifications (implements ``ICliNotifications``); see the module's docstring."""

    def __init__(
        self,
        *,
        file: str | bool | None = None,
        max_items: Optional[int] = None,
        max_age_days: Optional[float] = None,
        event_bus: Optional[Callable[[], Any]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._items: list[dict[str, Any]] = []
        self._file: str | None = None
        self._max_items = DEFAULT_NOTIFICATIONS_MAX_ITEMS
        self._max_age = timedelta(days=DEFAULT_NOTIFICATIONS_MAX_AGE_DAYS)
        self._event_bus: Optional[Callable[[], Any]] = None
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._tasks: set[asyncio.Future[Any]] = set()
        self.configure(file=file, max_items=max_items, max_age_days=max_age_days, event_bus=event_bus)

    def configure(
        self,
        *,
        file: str | bool | None = None,
        max_items: Optional[int] = None,
        max_age_days: Optional[float] = None,
        event_bus: Optional[Callable[[], Any]] = None,
    ) -> "CliNotificationService":
        """Sets where they are kept and the limits (``create_cli_server`` calls it once, before plugins publish).  The
        file's notifications are loaded; those published before (in memory) win over the file's of the same key.
        ``file=False`` keeps them in memory; ``None`` leaves the file as it is."""
        if max_items is not None:
            if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
                raise ValueError("notifications.max_items must be an integer >= 1")
            self._max_items = max_items
        if max_age_days is not None:
            if isinstance(max_age_days, bool) or not isinstance(max_age_days, (int, float)) or not max_age_days > 0:
                raise ValueError("notifications.max_age_days must be > 0")
            self._max_age = timedelta(days=max_age_days)
        if event_bus is not None:
            self._event_bus = event_bus
        if file is not None and file is not True:
            path = os.path.abspath(file) if isinstance(file, str) and file else None
            if path != self._file:
                self._file = path
                loaded = self._read_file()
                mine = {n["key"] for n in self._items}
                self._items.extend(n for n in loaded if n["key"] not in mine)
                if self._prune():
                    self._save()
        return self

    @property
    def path(self) -> Optional[str]:
        """The file they are kept in; ``None`` in memory."""
        return self._file

    # ------------------------------------------------------------------ sources

    def notify(self, notification: Any) -> dict[str, Any]:
        """Adds the notification of a key, or updates it: identical content changes nothing (no write, no event);
        changed content is unread (and shown) again for everyone.  Raises ``ValueError`` for invalid input."""
        n = normalize_cli_notification_input(notification)
        now = _iso(self._now())
        existing = self._find_key(n["key"])
        if existing is not None:
            if _content_of(existing) == _content_of(n):
                return self._view(existing, None)
            for key in ("kind", "severity", "title", "message"):
                existing[key] = n[key]
            for key in _OPTIONAL:
                if key in n:
                    existing[key] = n[key]
                else:
                    existing.pop(key, None)
            existing["updatedAt"] = now
            existing["readBy"] = []
            existing["dismissedBy"] = []
            self._changed("updated", existing)
            return self._view(existing, None)
        stored = {"id": f"n_{secrets.token_hex(8)}", **n, "createdAt": now, "updatedAt": now, "readBy": [], "dismissedBy": []}
        self._items.append(stored)
        self._prune()
        self._changed("created", stored)
        return self._view(stored, None)

    def resolve(self, key: str) -> bool:
        """Removes the notification of a key whose condition ended; true when there was one."""
        for i, n in enumerate(self._items):
            if n["key"] == key:
                del self._items[i]
                self._changed("resolved", n)
                return True
        return False

    def resolve_missing(self, prefix: str, keep: Iterable[str]) -> int:
        """Removes the notifications whose key starts with ``prefix`` and is not in ``keep``; returns how many."""
        kept = set(keep)
        gone = [n["key"] for n in self._items if n["key"].startswith(prefix) and n["key"] not in kept]
        for key in gone:
            self.resolve(key)
        return len(gone)

    def get(self, key: str) -> Optional[dict[str, Any]]:
        """The notification of a key, without per-user state; ``None`` when none."""
        n = self._find_key(key)
        return self._base(n) if n is not None else None

    # ------------------------------------------------------------------ readers

    def list(
        self, viewer: CliNotificationViewer, *, unread: bool = False, severity: Optional[str] = None, limit: Optional[int] = None,
    ) -> dict[str, Any]:
        """``{ items, unreadCount }``: what the viewer sees, newest first; ``unread`` only the unread ones,
        ``severity`` only that one, ``limit`` at most so many.  ``unreadCount`` ignores the filters."""
        visible = self._visible_to(viewer)
        items = [self._view(n, viewer.user_id) for n in visible]
        if unread:
            items = [n for n in items if not n["read"]]
        if severity:
            items = [n for n in items if n["severity"] == severity]
        if limit is not None and limit >= 0:
            items = items[:limit]
        return {"items": items, "unreadCount": sum(1 for n in visible if viewer.user_id not in n["readBy"])}

    def unread_count(self, viewer: CliNotificationViewer) -> int:
        """The viewer's unread notifications."""
        return sum(1 for n in self._visible_to(viewer) if viewer.user_id not in n["readBy"])

    def find(self, viewer: CliNotificationViewer, notification_id: str) -> Optional[dict[str, Any]]:
        """One notification as the viewer sees it; ``None`` when it does not exist, is hidden or was dismissed."""
        n = next((x for x in self._visible_to(viewer) if x["id"] == notification_id), None)
        return self._view(n, viewer.user_id) if n is not None else None

    def mark_read(self, viewer: CliNotificationViewer, notification_id: str) -> Optional[dict[str, Any]]:
        """Marks one read for the viewer; ``None`` when they cannot see it."""
        n = next((x for x in self._visible_to(viewer) if x["id"] == notification_id), None)
        if n is None:
            return None
        if viewer.user_id not in n["readBy"]:
            n["readBy"].append(viewer.user_id)
            self._changed("read", n, viewer.user_id)
        return self._view(n, viewer.user_id)

    def mark_all_read(self, viewer: CliNotificationViewer) -> int:
        """Marks every notification the viewer sees read; returns how many were unread."""
        unread = [n for n in self._visible_to(viewer) if viewer.user_id not in n["readBy"]]
        for n in unread:
            n["readBy"].append(viewer.user_id)
        if unread:
            self._changed("read-all", None, viewer.user_id)
        return len(unread)

    def dismiss(self, viewer: CliNotificationViewer, notification_id: str) -> bool:
        """Hides one from the viewer (it comes back when its content changes); false when they cannot see it."""
        n = next((x for x in self._visible_to(viewer) if x["id"] == notification_id), None)
        if n is None:
            return False
        n["dismissedBy"].append(viewer.user_id)
        if viewer.user_id not in n["readBy"]:
            n["readBy"].append(viewer.user_id)
        self._changed("dismissed", n, viewer.user_id)
        return True

    def clear(self, viewer: CliNotificationViewer, notification_id: str) -> bool:
        """Removes one for everyone (``notifications:manage``, checked by the caller); false when the viewer cannot
        see it."""
        for i, n in enumerate(self._items):
            if n["id"] == notification_id and self._can_see(viewer, n):
                del self._items[i]
                self._changed("cleared", n)
                return True
        return False

    def read_flag(self, notification_id: Optional[str], user_id: str) -> bool:
        """Whether ``user_id`` read the notification (the events socket personalises each message with it)."""
        n = next((x for x in self._items if x["id"] == notification_id), None) if notification_id else None
        return n is not None and user_id in n["readBy"]

    # ------------------------------------------------------------------ internals

    def _find_key(self, key: str) -> Optional[dict[str, Any]]:
        return next((n for n in self._items if n["key"] == key), None)

    @staticmethod
    def _can_see(viewer: CliNotificationViewer, n: dict[str, Any]) -> bool:
        permission = n.get("requiredPermission")
        if not permission:
            return True
        try:
            return bool(viewer.can(permission))
        except Exception:  # noqa: BLE001 - a failing check hides it
            return False

    def _visible_to(self, viewer: CliNotificationViewer) -> list[dict[str, Any]]:
        if self._prune():
            self._save()
        visible = [n for n in self._items if self._can_see(viewer, n) and viewer.user_id not in n["dismissedBy"]]
        return sorted(visible, key=lambda n: (n["updatedAt"], n["createdAt"]), reverse=True)

    @staticmethod
    def _base(n: dict[str, Any]) -> dict[str, Any]:
        return {k: copy.deepcopy(v) for k, v in n.items() if k not in ("readBy", "dismissedBy")}

    def _view(self, n: dict[str, Any], user_id: Optional[str]) -> dict[str, Any]:
        return {**self._base(n), "read": user_id is not None and user_id in n["readBy"]}

    def _prune(self) -> bool:
        """Drops the notifications past their age and over the cap (oldest first); true when it dropped some."""
        before = len(self._items)
        cutoff = self._now() - self._max_age
        self._items = [n for n in self._items if (_parse(n["updatedAt"]) or cutoff) >= cutoff]
        if len(self._items) > self._max_items:
            # The oldest change goes first; of the same instant, the one added first
            ranked = sorted(enumerate(self._items), key=lambda e: (e[1]["updatedAt"], e[0]), reverse=True)
            self._items = [n for _, n in ranked[: self._max_items]]
        return len(self._items) != before

    def _changed(self, action: str, n: Optional[dict[str, Any]], user_id: Optional[str] = None) -> None:
        self._save()
        bus = self._event_bus() if self._event_bus is not None else None
        if bus is None:
            return
        data: dict[str, Any] = {"action": action}
        if n is not None:
            data["id"] = n["id"]
            data["key"] = n["key"]
            if action not in ("resolved", "cleared"):
                data["item"] = self._base(n)
        # Read and dismiss concern one user; the rest everyone who may see the notification
        if user_id is not None:
            audience = CliEventAudience(user_id=user_id)
        elif n is not None and n.get("requiredPermission"):
            audience = CliEventAudience(permission=n["requiredPermission"])
        else:
            audience = CliEventAudience()
        self._publish(bus, data, audience)

    def _publish(self, bus: Any, data: dict[str, Any], audience: CliEventAudience) -> None:
        try:
            result = bus.publish(CLI_NOTIFICATION_CHANGED_EVENT, data, audience)
        except Exception as exc:  # noqa: BLE001 - the notification changed; the event failing is logged
            logger.error("Publishing %s failed: %s", CLI_NOTIFICATION_CHANGED_EVENT, exc)
            return
        if not inspect.isawaitable(result):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop (published from synchronous host code): delivered right away
            try:
                asyncio.run(_awaited(result))
            except Exception as exc:  # noqa: BLE001
                logger.error("Publishing %s failed: %s", CLI_NOTIFICATION_CHANGED_EVENT, exc)
            return
        task = asyncio.ensure_future(result)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _read_file(self) -> list[dict[str, Any]]:
        if not self._file or not os.path.isfile(self._file):
            return []
        try:
            with open(self._file, encoding="utf-8") as f:
                parsed = json.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reading %s failed, starting without notifications: %s", self._file, exc)
            return []
        raw_items = parsed.get("items") if isinstance(parsed, dict) else None
        if not isinstance(raw_items, list):
            return []
        items: list[dict[str, Any]] = []
        for raw in raw_items:
            try:
                n = normalize_cli_notification_input(raw)
            except ValueError:
                continue  # a damaged entry is dropped
            ids = (raw.get("id"), raw.get("createdAt"), raw.get("updatedAt"))
            if not all(isinstance(x, str) and x for x in ids) or _parse(raw["updatedAt"]) is None:
                continue
            items.append({
                "id": raw["id"], **n, "createdAt": raw["createdAt"], "updatedAt": raw["updatedAt"],
                "readBy": [x for x in raw.get("readBy") or [] if isinstance(x, str)],
                "dismissedBy": [x for x in raw.get("dismissedBy") or [] if isinstance(x, str)],
            })
        return items

    def _save(self) -> None:
        """Writes the file atomically (a temporary file renamed over it)."""
        if not self._file:
            return
        path = self._file
        temp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(temp, "w", encoding="utf-8") as out:
                out.write(json.dumps({"version": 1, "items": self._items}, indent=2, ensure_ascii=False) + "\n")
            os.replace(temp, path)
        except OSError as exc:
            logger.warning("Saving %s failed: %s", path, exc)
            try:
                os.remove(temp)
            except OSError:
                pass


async def _awaited(value: Any) -> Any:
    return await value
