"""The update checker: dashboard and server plugins installed from the marketplace with a newer version (updated
here), plugins the host adds in code (updated in code), and the server package (``qodalis-cli-server`` on PyPI).
Every update becomes a notification (``update:…``, permission ``updates:read``) resolved once it no longer applies.
Runs every 6 hours (a job ``updates:check`` of the jobs plugin when one is mounted, else a task) and on demand
(``POST /api/v1/qcli/updates/check``); the last result is kept in ``<data_dir>/updates.json``.  The same contract
and JSON as the Node and .NET servers."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote

from qodalis_cli_server_abstractions import CliJobOptions, ICliJob

from .dashboard_marketplace import MarketplaceFetch, compare_dashboard_plugin_versions
from .server_plugin_marketplace import (
    DEFAULT_PYPI_URL,
    SERVER_CORE_PACKAGE,
    SERVER_PLUGIN_PLATFORM,
    compare_server_plugin_versions,
)

logger = logging.getLogger("qcli-admin")

#: Seeing the available updates and checking for them (``UPDATES_READ_PERMISSION`` of the abstractions; kept here
#: so the plugin runs with abstractions released before it).
UPDATES_READ_PERMISSION = "updates:read"
#: The keys of the update notifications start with it (``UPDATE_NOTIFICATION_PREFIX``).
UPDATE_NOTIFICATION_PREFIX = "update:"

#: How often the server checks for updates, in seconds.
UPDATE_CHECK_INTERVAL = 6 * 3600.0
#: The first check after a start, in seconds (unless the last result is fresher than the interval).
UPDATE_CHECK_INITIAL_DELAY = 60.0
#: The job id when the jobs plugin schedules the check.
UPDATE_CHECK_JOB_ID = "updates:check"
#: The file the last result is kept in, below the data directory.
UPDATES_FILE = "updates.json"
#: The dashboard tab the update notifications open.
UPDATES_LINK = "/plugins?tab=updates"
#: Where an update of the server package comes from (``npm`` on Node).
SERVER_PACKAGE_SOURCE = "pypi"
#: Why the server package is updated in code.
SERVER_PACKAGE_REASON = "update the server packages in code (every qodalis-cli-server* package shares the version)"
#: Most marketplace pages read per kind (100 plugins each).
_MAX_PAGES = 10
_PAGE_SIZE = 100


@dataclass
class UpdateCheckOptions:
    """The periodic check (``CliAdminBuilder.set_update_checks``)."""

    #: Check periodically (default true); "Check now" works either way.
    enabled: bool = True
    #: Seconds between checks (default 6 hours).
    interval: Optional[float] = None
    #: Seconds until the first check after a start (default 60; a fresher last result waits until it is due).
    initial_delay: Optional[float] = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class UpdateCheckerDeps:
    """What the checker reads and where it reports."""

    #: The running server's version (``qodalis-cli-server``).
    server_version: Callable[[], Optional[str]]
    #: The HTTP client of the package index (``await fetch(url, headers, timeout)``).
    fetch: MarketplaceFetch
    #: The dashboard marketplace (``search``, ``records``), when on.
    dashboard: Any = None
    #: The server marketplace (``search``, ``installed_records``), when on.
    server: Any = None
    #: The package index (default ``https://pypi.org``): ``<pypi_url>/pypi/qodalis-cli-server/json``.
    pypi_url: str = DEFAULT_PYPI_URL
    timeout: float = 15.0
    #: The server's notifications (``builder.notifications``), read on every check.
    notifications: Callable[[], Any] = lambda: None
    #: The jobs plugin's scheduler when one is mounted (read when the checks start).
    scheduler: Callable[[], Any] = lambda: None
    #: The file the last result is kept in (``None``: memory only).
    file: Optional[str] = None
    now: Callable[[], datetime] = field(default=_utc_now)


def _empty_result() -> dict[str, Any]:
    return {
        "checkedAt": None, "checking": False, "server": None,
        "dashboardPlugins": [], "serverPlugins": [], "codePlugins": [], "count": 0,
    }


def _text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _count(result: Mapping[str, Any]) -> int:
    server = result.get("server") or {}
    return (
        len(result["dashboardPlugins"]) + len(result["serverPlugins"]) + len(result["codePlugins"])
        + (1 if server.get("updateAvailable") else 0)
    )


def _newer(latest: Optional[str], current: Optional[str]) -> bool:
    return bool(current and latest) and compare_server_plugin_versions(latest, current) > 0  # type: ignore[arg-type]


def update_notification_key(item: Mapping[str, Any]) -> str:
    """``update:dashboard:<name>``, ``update:server:<name>``, ``update:code:<kind>:<package>``,
    ``update:core:<package>``."""
    prefix = UPDATE_NOTIFICATION_PREFIX
    if item["kind"] == "core":
        return f"{prefix}core:{item['package']}"
    if item.get("source") == "code":
        return f"{prefix}code:{item['kind']}:{item['package']}"
    return f"{prefix}{item['kind']}:{item['name']}"


def _install_command(platform: str, package: str, latest: Optional[str]) -> str:
    if platform == "python":
        return f"pip install --upgrade {package}{f'=={latest}' if latest else ''}"
    if platform == "dotnet":
        return f"dotnet add package {package}{f' --version {latest}' if latest else ''}"
    if platform == "node":
        return f"npm install {package}{f'@{latest}' if latest else ''}"
    return f"{package}{f'@{latest}' if latest else ''}"


def install_commands(platforms: Any, package: str, latest: Optional[str]) -> dict[str, str]:
    """The install commands per platform of a marketplace server plugin: this server's first (at ``latest``), then the
    other platforms' (their ``install`` when the marketplace gives one, else the usual command)."""
    if isinstance(platforms, list):
        entries = [p for p in platforms if isinstance(p, Mapping)]
    elif isinstance(platforms, Mapping):
        entries = [{**(p if isinstance(p, Mapping) else {}), "platform": name} for name, p in platforms.items()]
    else:
        entries = []
    out = {SERVER_PLUGIN_PLATFORM: _install_command(SERVER_PLUGIN_PLATFORM, package, latest)}
    for p in entries:
        platform, other = _text(p.get("platform")), _text(p.get("package"))
        if not platform or platform == SERVER_PLUGIN_PLATFORM or not other:
            continue
        out[platform] = _text(p.get("install")) or _install_command(platform, other, _text(p.get("latest")))
    return out


class _UpdateCheckJob(ICliJob):
    """The ``updates:check`` job of the jobs plugin."""

    def __init__(self, checker: "UpdateChecker") -> None:
        self._checker = checker

    async def execute_async(self, context: Any, cancellation_event: asyncio.Event) -> None:
        result = await self._checker.check()
        error = result.get("error")
        context.logger.info(f"{result['count']} update(s) available{f' ({error})' if error else ''}")
        if error:
            raise RuntimeError(error)


class UpdateChecker:
    """Checks the marketplace (and PyPI for the server package) for newer versions; see the module's docstring."""

    def __init__(self, deps: UpdateCheckerDeps, options: Optional[UpdateCheckOptions] = None) -> None:
        self.deps = deps
        self.options = options or UpdateCheckOptions()
        self._last: dict[str, Any] = self._read_file() or _empty_result()
        self._running: Optional[asyncio.Future[dict[str, Any]]] = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._started = False

    # ------------------------------------------------------------------ reading and checking

    def result(self) -> dict[str, Any]:
        """The last result (``GET /api/v1/qcli/updates``)."""
        out = copy.deepcopy(self._last)
        out["checking"] = self._running is not None and not self._running.done()
        return out

    async def check(self) -> dict[str, Any]:
        """Checks now; a check already running is joined."""
        if self._running is None or self._running.done():
            self._running = asyncio.ensure_future(self._run())
        running = self._running
        try:
            await asyncio.shield(running)
        finally:
            if running.done() and self._running is running:
                self._running = None
        return self.result()

    # ------------------------------------------------------------------ schedule

    def start(self) -> None:
        """Starts the periodic checks (in the app's event loop): the first after ``initial_delay`` (or when the last
        result is due), then every ``interval``, through the jobs plugin's scheduler when there is one."""
        if self._started or self.options.enabled is False:
            return
        self._started = True
        interval = float(self.options.interval if self.options.interval is not None else UPDATE_CHECK_INTERVAL)
        initial = float(self.options.initial_delay if self.options.initial_delay is not None else UPDATE_CHECK_INITIAL_DELAY)
        checked_at = self._last.get("checkedAt")
        first = initial
        if isinstance(checked_at, str):
            try:
                age = (self.deps.now() - datetime.fromisoformat(checked_at.replace("Z", "+00:00"))).total_seconds()
                first = max(initial, interval - age)
            except ValueError:
                pass
        self._tasks.append(asyncio.ensure_future(self._after(first)))
        scheduler = self.deps.scheduler()
        if scheduler is not None and interval >= 1 and float(interval).is_integer():
            try:
                jobs = getattr(scheduler, "jobs", None) or {}
                if UPDATE_CHECK_JOB_ID not in jobs:
                    scheduler.register(_UpdateCheckJob(self), CliJobOptions(
                        id=UPDATE_CHECK_JOB_ID,
                        name=UPDATE_CHECK_JOB_ID,
                        description="Checks the marketplace and PyPI for newer versions of the plugins and the server",
                        group="system",
                        tags=["system", "updates"],
                        interval=f"{int(interval)}s",
                    ))
                return
            except Exception as exc:  # noqa: BLE001 - the scheduler refused it: a task instead
                logger.warning("the jobs plugin refused the update check job (%s); checking on a timer", exc)
        self._tasks.append(asyncio.ensure_future(self._every(interval)))

    async def stop(self) -> None:
        """Stops the periodic checks (a job registered with the jobs plugin stays: the scheduler stops it)."""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._started = False

    async def _after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._quiet_check()

    async def _every(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            await self._quiet_check()

    async def _quiet_check(self) -> None:
        try:
            await self.check()
        except Exception as exc:  # noqa: BLE001
            logger.warning("the update check failed: %s", exc)

    # ------------------------------------------------------------------ after a change

    def refresh_installed(self) -> None:
        """After an install, update or removal: drops the marketplace updates whose installed version caught up (or
        that were removed) and resolves their notifications, without asking the marketplace again."""
        dashboard = self.deps.dashboard
        server = self.deps.server
        installed_dashboard = {r["name"]: r["version"] for r in (dashboard.records() if dashboard is not None else [])}
        installed_server = {r["name"]: r["version"] for r in (server.installed_records() if server is not None else [])}

        def stale(item: Mapping[str, Any], versions: Mapping[str, str], compare: Callable[[str, str], int]) -> bool:
            current = versions.get(item["name"])
            return not current or not item.get("latest") or compare(item["latest"], current) <= 0

        gone = [i for i in self._last["dashboardPlugins"] if stale(i, installed_dashboard, compare_dashboard_plugin_versions)]
        gone += [i for i in self._last["serverPlugins"] if stale(i, installed_server, compare_server_plugin_versions)]
        if not gone:
            return
        self._last["dashboardPlugins"] = [i for i in self._last["dashboardPlugins"] if i not in gone]
        self._last["serverPlugins"] = [i for i in self._last["serverPlugins"] if i not in gone]
        self._last["count"] = _count(self._last)
        notifications = self.deps.notifications()
        if notifications is not None:
            for item in gone:
                notifications.resolve(update_notification_key(item))
        self._save()

    # ------------------------------------------------------------------ the check

    async def _run(self) -> dict[str, Any]:
        errors: list[str] = []
        # The categories that answered: only their notifications are resolved
        answered: list[str] = []
        prefix = UPDATE_NOTIFICATION_PREFIX
        nxt = _empty_result()

        if self.deps.dashboard is not None:
            try:
                for item in await self._items(lambda page: self.deps.dashboard.search(page=page, page_size=_PAGE_SIZE)):
                    installed = item.get("installed")
                    if not isinstance(installed, Mapping) or not item.get("updateAvailable"):
                        continue
                    latest = _text(item.get("version")) or _text(item.get("latest"))
                    base = {
                        "kind": "dashboard", "name": item["name"], "package": item["name"],
                        "title": _text(item.get("title")) or item["name"], "icon": _text(item.get("icon")),
                        "current": installed.get("version"), "latest": latest, "updateAvailable": True,
                    }
                    if installed.get("source") == "marketplace":
                        nxt["dashboardPlugins"].append(self._marketplace_item(base, item))
                    else:
                        nxt["codePlugins"].append({
                            **base, "source": "code", "canUpdate": False, "reason": "update in code",
                            "install": {"node": _install_command("node", item["name"], latest)},
                        })
                answered += [f"{prefix}dashboard:", f"{prefix}code:dashboard:"]
            except Exception as exc:  # noqa: BLE001
                errors.append(f"dashboard plugins: {_reason(exc)}")

        if self.deps.server is not None:
            try:
                for item in await self._items(
                    lambda page: self.deps.server.search(page=page, page_size=_PAGE_SIZE, platform="this"),
                ):
                    installed = item.get("installed")
                    platform = item.get("platform")
                    if not isinstance(installed, Mapping) or not item.get("updateAvailable") or not isinstance(platform, Mapping):
                        continue
                    base = {
                        "kind": "server", "name": item["name"], "package": platform.get("package"),
                        "title": _text(item.get("title")) or item["name"], "icon": _text(item.get("icon")),
                        "current": installed.get("version"), "latest": platform.get("latest"), "updateAvailable": True,
                    }
                    if installed.get("source") == "marketplace":
                        nxt["serverPlugins"].append(self._marketplace_item(base, item))
                    else:
                        nxt["codePlugins"].append({
                            **base, "source": "code", "canUpdate": False, "reason": "update in code",
                            "install": install_commands(item.get("platforms"), base["package"], base["latest"]),
                        })
                answered += [f"{prefix}server:", f"{prefix}code:server:"]
            except Exception as exc:  # noqa: BLE001
                errors.append(f"server plugins: {_reason(exc)}")

        current = self.deps.server_version()
        try:
            latest = await self._pypi_latest(SERVER_CORE_PACKAGE)
            nxt["server"] = self._server_item(current, latest)
            answered.append(f"{prefix}core:")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"server package: {_reason(exc)}")
            last = self._last.get("server")
            nxt["server"] = {**last, "current": current} if isinstance(last, dict) else None

        # A category that did not answer keeps its last items
        if f"{prefix}dashboard:" not in answered:
            nxt["dashboardPlugins"] = self._last["dashboardPlugins"]
            nxt["codePlugins"] += [i for i in self._last["codePlugins"] if i.get("kind") == "dashboard"]
        if f"{prefix}server:" not in answered:
            nxt["serverPlugins"] = self._last["serverPlugins"]
            nxt["codePlugins"] += [i for i in self._last["codePlugins"] if i.get("kind") == "server"]
        nxt["checkedAt"] = _iso(self.deps.now())
        if errors:
            nxt["error"] = "; ".join(errors)
        nxt["count"] = _count(nxt)
        self._last = nxt
        self._save()
        self._publish(nxt, answered)
        return nxt

    @staticmethod
    def _marketplace_item(base: dict[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
        out = {**base, "source": "marketplace", "canUpdate": item.get("allowed") is True}
        if _text(item.get("blockedReason")):
            out["reason"] = item["blockedReason"]
        return out

    @staticmethod
    def _server_item(current: Optional[str], latest: Optional[str]) -> dict[str, Any]:
        return {
            "kind": "core", "name": SERVER_CORE_PACKAGE, "package": SERVER_CORE_PACKAGE, "title": "Server",
            "current": current, "latest": latest, "source": SERVER_PACKAGE_SOURCE,
            "updateAvailable": _newer(latest, current), "canUpdate": False, "reason": SERVER_PACKAGE_REASON,
            "install": {SERVER_PLUGIN_PLATFORM: _install_command(SERVER_PLUGIN_PLATFORM, SERVER_CORE_PACKAGE, latest)},
        }

    def _publish(self, result: Mapping[str, Any], answered: list[str]) -> None:
        """One notification per update; those of the categories that answered and no longer apply are resolved."""
        notifications = self.deps.notifications()
        if notifications is None:
            return
        server = result.get("server")
        items = [
            *result["dashboardPlugins"], *result["serverPlugins"], *result["codePlugins"],
            *([server] if isinstance(server, dict) and server.get("updateAvailable") else []),
        ]
        keys: list[str] = []
        for item in items:
            key = update_notification_key(item)
            keys.append(key)
            versions = f"{item.get('current') or '?'} → {item.get('latest') or '?'}"
            if item["kind"] == "core":
                title = f"Server update available: {item.get('latest')}"
                message = f"{item['package']} {versions}. Update the server packages in code and restart."
            elif item.get("source") == "code":
                title = f"Update in code: {item['title']} {item.get('latest')}"
                command = next(iter((item.get("install") or {}).values()), None) or "update its package"
                message = f"{item['package']} {versions}. The host adds it in code: {command}, then restart."
            else:
                title = f"Update available: {item['title']} {item.get('latest')}"
                message = f"{item['package']} {versions}.{'' if item.get('canUpdate') else ' ' + (item.get('reason') or '')}".strip()
            try:
                notifications.notify({
                    "key": key, "kind": "update", "severity": "info", "title": title[:200], "message": message,
                    "link": UPDATES_LINK, "requiredPermission": UPDATES_READ_PERMISSION,
                    "data": {
                        "kind": item["kind"], "name": item["name"], "package": item["package"], "current": item.get("current"),
                        "latest": item.get("latest"), "source": item.get("source"), "canUpdate": item.get("canUpdate"),
                    },
                })
            except ValueError:
                pass  # an item the notification rules refuse (a huge name): listed by GET /updates all the same
        for p in answered:
            notifications.resolve_missing(p, keys)

    @staticmethod
    async def _items(search: Callable[[int], Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            res = await search(page)
            items = [i for i in res.get("items") or [] if isinstance(i, dict) and isinstance(i.get("name"), str)]
            out += items
            total = res.get("total")
            if not items or not isinstance(total, int) or len(out) >= total:
                break
        return out

    async def _pypi_latest(self, package: str) -> Optional[str]:
        """``info.version`` of a project on the package index (``<pypi_url>/pypi/<package>/json``)."""
        url = f"{self.deps.pypi_url.rstrip('/')}/pypi/{quote(package, safe='')}/json"
        try:
            res = await self.deps.fetch(url, {"Accept": "application/json"}, self.deps.timeout)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("package index unreachable") from exc
        if res.status == 404:
            return None
        if not res.ok:
            raise RuntimeError("package index unreachable")
        try:
            body = res.json()
        except Exception:  # noqa: BLE001
            return None
        info = body.get("info") if isinstance(body, dict) else None
        return _text(info.get("version")) if isinstance(info, dict) else None

    # ------------------------------------------------------------------ the file

    def _read_file(self) -> Optional[dict[str, Any]]:
        path = self.deps.file
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                parsed = json.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reading %s failed: %s", path, exc)
            return None
        if not isinstance(parsed, dict):
            return None

        def items(value: Any) -> list[dict[str, Any]]:
            return [i for i in value if isinstance(i, dict) and isinstance(i.get("name"), str)] if isinstance(value, list) else []

        result = _empty_result()
        result["checkedAt"] = parsed.get("checkedAt") if isinstance(parsed.get("checkedAt"), str) else None
        if isinstance(parsed.get("error"), str):
            result["error"] = parsed["error"]
        result["dashboardPlugins"] = items(parsed.get("dashboardPlugins"))
        result["serverPlugins"] = items(parsed.get("serverPlugins"))
        result["codePlugins"] = items(parsed.get("codePlugins"))
        server = parsed.get("server")
        if isinstance(server, dict):
            # The running version may have changed since (an update applied in code)
            current = self.deps.server_version()
            result["server"] = {**server, "current": current, "updateAvailable": _newer(server.get("latest"), current)}
        result["count"] = _count(result)
        return result

    def _save(self) -> None:
        path = self.deps.file
        if not path:
            return
        temp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            body = {k: v for k, v in self._last.items() if k != "checking"}
            with open(temp, "w", encoding="utf-8") as out:
                out.write(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
            os.replace(temp, path)
        except OSError as exc:  # an unwritable data directory: the result stays in memory
            logger.debug("saving %s failed: %s", path, exc)
            try:
                os.remove(temp)
            except OSError:
                pass


def _reason(exc: BaseException) -> str:
    return getattr(exc, "message", None) or str(exc) or type(exc).__name__


__all__ = [
    "SERVER_PACKAGE_REASON",
    "SERVER_PACKAGE_SOURCE",
    "UPDATES_FILE",
    "UPDATES_LINK",
    "UPDATES_READ_PERMISSION",
    "UPDATE_NOTIFICATION_PREFIX",
    "UPDATE_CHECK_INITIAL_DELAY",
    "UPDATE_CHECK_INTERVAL",
    "UPDATE_CHECK_JOB_ID",
    "UpdateCheckOptions",
    "UpdateChecker",
    "UpdateCheckerDeps",
    "install_commands",
    "update_notification_key",
]
