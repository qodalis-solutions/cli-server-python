"""Dashboard plugins from the marketplace: browse its ``kind=dashboard`` catalog, install, update and remove plugins
at runtime (bundles from jsDelivr then unpkg, checked against the marketplace's sha384 before anything is stored),
keep them in ``<dir>/<name>/<version>/`` with ``<dir>/installed.json`` (written atomically), register each as a
dashboard extension and serve its files at ``/qcli/modules/<id>/``.  The same contract and JSON as the Node server."""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional
from urllib.parse import quote, urlencode

from qodalis_cli_server_abstractions.audit import AuditEntry
from qodalis_cli_server_abstractions.config import validate_cli_config_value
from qodalis_cli_server_abstractions.dashboard_modules import DASHBOARD_MODULES_PREFIX, dashboard_module_page

from .dashboard_plugin_config import (
    DASHBOARD_PLUGINS_CONFIG_PROVIDER_ID,
    DashboardPluginConfig,
    DashboardPluginsConfigProvider,
    parse_dashboard_plugin_config,
    validate_dashboard_plugin_config_values,
)

logger = logging.getLogger("qcli-admin")

#: The dashboard extension API this server's dashboard implements; versions needing a newer one are refused.
DASHBOARD_API_VERSION = 1
DEFAULT_MARKETPLACE_URL = "https://plugins.qodalis.com"
#: Where installed dashboard plugins are kept, relative to the working directory.
DEFAULT_DASHBOARD_PLUGINS_DIR = os.path.join("qcli-data", "dashboard-plugins")
#: CDNs the bundles are downloaded from, in order (jsDelivr, then unpkg).
DEFAULT_DASHBOARD_PLUGIN_CDNS: tuple[str, ...] = ("https://cdn.jsdelivr.net/npm", "https://unpkg.com")
#: The most bytes one file of a dashboard plugin may have.
DASHBOARD_PLUGIN_MAX_FILE_BYTES = 5 * 1024 * 1024
#: The public API of the marketplace, below its URL.
MARKETPLACE_API_PATH = "/api/v1/qcli/marketplace"
#: The event published when a marketplace plugin is installed, updated or removed (audience ``admin:access``).
DASHBOARD_EXTENSIONS_CHANGED_EVENT = "dashboard:extensions-changed"
DASHBOARD_INSTALL_PERMISSION = "dashboard:install"
INSTALL_POLICIES = ("verified", "listed", "off")

_NPM_NAME = re.compile(r"(?:@[a-z0-9\-~][a-z0-9\-._~]*/)?[a-z0-9\-~][a-z0-9\-._~]*")
_SEMVER = re.compile(
    r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_EXTENSION_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_OFFICIAL_MODULE = re.compile(r"@qodalis/cli-dashboard-module-([a-z0-9][a-z0-9-]{0,39})")
_FILE_PATH = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*")
_INTEGRITY = re.compile(r"sha384-[A-Za-z0-9+/]+={0,2}")
_SERVED = re.compile(r"/([a-z0-9][a-z0-9-]{0,39})(/.*)?")
INSTALLED_FILE = "installed.json"


@dataclass
class MarketplaceResponse:
    """What the marketplace client reads of an HTTP response."""

    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


#: The HTTP client of the marketplace and the CDNs: ``await fetch(url, headers, timeout)`` (tests pass a fake).
MarketplaceFetch = Callable[[str, Mapping[str, str], float], Awaitable[MarketplaceResponse]]


async def default_marketplace_fetch(url: str, headers: Mapping[str, str], timeout: float) -> MarketplaceResponse:
    """GETs ``url`` with the standard library (in a thread); reads at most 5 MB and a byte."""

    def get() -> MarketplaceResponse:
        request = urllib.request.Request(url, headers={"User-Agent": "qodalis-cli-server-python", **headers})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as res:  # noqa: S310 - https URLs of the options
                return MarketplaceResponse(
                    res.status, res.read(DASHBOARD_PLUGIN_MAX_FILE_BYTES + 1), {k.lower(): v for k, v in res.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            return MarketplaceResponse(exc.code, b"", {})

    return await asyncio.to_thread(get)


class DashboardMarketplaceError(Exception):
    """A refused or failed marketplace operation: the HTTP status and the ``{ error }`` text (``errors``: per-key
    messages of settings)."""

    def __init__(self, status: int, message: str, errors: Optional[dict[str, str]] = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.errors = errors


@dataclass
class DashboardMarketplaceActor:
    """Who installs or removes a plugin (``installedBy`` and the audit log)."""

    user_name: str
    user_id: Optional[str] = None


@dataclass
class DashboardMarketplaceOptions:
    """The admin plugin's marketplace options (``CliAdminBuilder.set_marketplace``)."""

    #: The marketplace (default ``https://plugins.qodalis.com``).
    url: Optional[str] = None
    #: ``verified`` (default): listed versions of verified publishers only; ``listed``: any listed version;
    #: ``off``: browse only.
    install: Optional[str] = None
    #: Where installed plugins are kept (default ``./qcli-data/dashboard-plugins``, from the working directory).
    dir: Optional[str] = None
    #: The HTTP client for the marketplace and the CDNs (default: the standard library's).
    fetch: Optional[MarketplaceFetch] = None
    #: The CDNs bundles are downloaded from, in order (default jsDelivr, then unpkg).
    cdns: Optional[list[str]] = None
    #: Timeout of each request in seconds (default 15).
    timeout: float = 15.0
    #: Server plugins: the package index (default ``https://pypi.org``).
    pypi_url: Optional[str] = None
    #: Server plugins: runs pip (default ``sys.executable -m pip``; tests pass a fake).
    pip: Optional[Callable[[list[str]], Awaitable[Any]]] = None
    #: Server plugins: the HTTP client of wheel downloads (default: ``fetch`` when given, else the standard library's).
    download: Optional[MarketplaceFetch] = None
    #: Server plugins: the server's version (default: the installed ``qodalis-cli-server``'s).
    server_version: Optional[str] = None


@dataclass
class DashboardMarketplaceDeps:
    """Where the plugins' pages go and what the marketplace reports to."""

    #: The registered extensions' ids, now.
    extension_ids: Callable[[], list[str]]
    #: Adds a dashboard extension (raises ``ValueError`` when the id is taken or it is invalid).
    add_extension: Callable[[Mapping[str, Any]], Any]
    #: Removes a dashboard extension by id.
    remove_extension: Callable[[str], bool]
    #: The server plugin packages installed (``GET /plugins/installed``), for ``requires.installed``.
    server_plugins: Callable[[], list[str]] = lambda: []
    #: The audit sink, read on every change (an async function taking an ``AuditEntry``).
    audit: Callable[[], Any] = lambda: None
    #: Publishes an event to holders of ``admin:access``.
    publish: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]] = None
    #: Publishes ``config:changed`` (audience ``config:read``) when an install, update or removal changes stored settings.
    publish_config_changed: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None
    #: Resolves once the config providers have their stores (``builder.config.ready``): awaited before the settings
    #: are read or written, so they go to the store the server resolved for ``dashboard-plugins``.
    config_ready: Optional[Callable[[], Awaitable[None]]] = None


def is_valid_dashboard_plugin_name(name: Any) -> bool:
    """True for a valid npm package name (lower case, optionally scoped, at most 214 characters)."""
    return isinstance(name, str) and len(name) <= 214 and _NPM_NAME.fullmatch(name) is not None


def is_valid_dashboard_plugin_version(version: Any) -> bool:
    """True for a semver version (``1.2.3``, ``1.2.3-beta.1``)."""
    return isinstance(version, str) and len(version) <= 64 and _SEMVER.fullmatch(version) is not None


def dashboard_plugin_dir_name(name: str) -> str:
    """The directory a package is kept in: the name with the scope's ``/`` as ``+`` (``@acme/tickets`` ->
    ``@acme+tickets``), which no npm name contains, so names never clash or leave the storage directory.
    Raises 400 for an invalid name."""
    if not is_valid_dashboard_plugin_name(name):
        raise DashboardMarketplaceError(400, "invalid package name")
    return name.replace("/", "+", 1)


def dashboard_plugin_id(name: str, extension_id: Any = None) -> Optional[str]:
    """The dashboard extension id of a package: ``@qodalis/cli-dashboard-module-<id>`` -> ``<id>``, else the
    extension's own id (the marketplace record's ``extension.id``), else the package's own name (scope dropped,
    ``cli-dashboard-`` and ``module-``/``plugin-`` prefixes dropped, other characters as ``-``, at most 40).
    ``None`` when nothing fits."""
    official = _OFFICIAL_MODULE.fullmatch(name)
    if official:
        return official.group(1)
    if isinstance(extension_id, str) and _EXTENSION_ID.fullmatch(extension_id):
        return extension_id
    base = name.split("/")[-1]
    base = re.sub(r"^cli-dashboard-(?:module-|plugin-)?", "", base)
    base = re.sub(r"[^a-z0-9-]+", "-", base)
    base = re.sub(r"^-+", "", base)[:40]
    base = re.sub(r"-+$", "", base)
    return base if _EXTENSION_ID.fullmatch(base) else None


def compare_dashboard_plugin_versions(a: str, b: str) -> int:
    """Compares two semver versions (prereleases before their release); a non-semver text compares as text."""
    pa, pb = _SEMVER.fullmatch(a), _SEMVER.fullmatch(b)
    if not pa or not pb:
        return -1 if a < b else 1 if a > b else 0
    for i in (1, 2, 3):
        d = int(pa.group(i)) - int(pb.group(i))
        if d:
            return 1 if d > 0 else -1
    xa, xb = pa.group(4), pb.group(4)
    if not xa and not xb:
        return 0
    if not xa:
        return 1
    if not xb:
        return -1
    sa, sb = xa.split("."), xb.split(".")
    for i in range(max(len(sa), len(sb))):
        if i >= len(sa):
            return -1
        if i >= len(sb):
            return 1
        na, nb = sa[i].isdigit(), sb[i].isdigit()
        if na and nb:
            d = int(sa[i]) - int(sb[i])
            if d:
                return 1 if d > 0 else -1
        elif na != nb:
            return -1 if na else 1
        elif sa[i] != sb[i]:
            return -1 if sa[i] < sb[i] else 1
    return 0


def sha384_integrity(data: bytes) -> str:
    """``sha384-<base64>`` of the bytes (Subresource Integrity)."""
    return "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode("ascii")


def python_server_package(server_packages: Any) -> Optional[str]:
    """The Python server package a plugin needs: ``serverPackages.python`` (``{node, python, dotnet}``), or the
    ``pypi`` entry of the marketplace's list form (``[{"ecosystem": "pypi", "name": ...}]``)."""
    if isinstance(server_packages, list):
        entry = next((p for p in server_packages if isinstance(p, dict) and p.get("ecosystem") == "pypi"), None)
        name = entry.get("name") if entry else None
        return name if isinstance(name, str) and name else None
    if isinstance(server_packages, dict):
        name = server_packages.get("python")
        return name if isinstance(name, str) and name else None
    return None


_VERSION_KEY = functools.cmp_to_key(compare_dashboard_plugin_versions)


def _api_of(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1


def _is_dashboard_version(v: Mapping[str, Any], plugin: Mapping[str, Any]) -> bool:
    """True when a version record is a dashboard bundle (its own ``kind``, else the plugin's)."""
    return (v.get("kind") if v.get("kind") is not None else plugin.get("kind")) == "dashboard"


def _listed_versions(plugin: Mapping[str, Any]) -> list[dict[str, Any]]:
    versions = [
        v for v in plugin.get("versions") or []
        if isinstance(v, dict) and is_valid_dashboard_plugin_version(v.get("version"))
        and v.get("status") == "listed" and _is_dashboard_version(v, plugin)
    ]
    return sorted(versions, key=lambda v: _VERSION_KEY(v["version"]), reverse=True)


def _latest_fitting(plugin: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The newest listed version this dashboard can run."""
    return next((v for v in _listed_versions(plugin) if _api_of(v.get("dashboardApi")) <= DASHBOARD_API_VERSION), None)


def _latest_listed(plugin: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The newest listed version, whatever API it needs."""
    versions = _listed_versions(plugin)
    return versions[0] if versions else None


def _planned_files(files: Any) -> list[dict[str, Any]]:
    """The files to store: exactly one ``.js`` (as ``extension.js``) and at most one ``.css`` (``extension.css``)."""
    invalid = DashboardMarketplaceError(502, "invalid marketplace record: files")
    if not isinstance(files, list) or not files or len(files) > 2:
        raise invalid
    out: list[dict[str, Any]] = []
    for f in files:
        path = f.get("path") if isinstance(f, dict) else None
        if not isinstance(path, str) or not _FILE_PATH.fullmatch(path) or any(s in (".", "..") for s in path.split("/")):
            raise invalid
        integrity = f.get("integrity")
        if not isinstance(integrity, str) or not _INTEGRITY.fullmatch(integrity):
            raise invalid
        file = "extension.js" if path.endswith(".js") else "extension.css" if path.endswith(".css") else None
        if file is None or any(o["file"] == file for o in out):
            raise invalid
        size = f.get("size")
        if size is not None and (not isinstance(size, (int, float)) or isinstance(size, bool) or size < 0):
            raise invalid
        if isinstance(size, (int, float)) and size > DASHBOARD_PLUGIN_MAX_FILE_BYTES:
            raise DashboardMarketplaceError(502, f"file too large: {path}")
        entry: dict[str, Any] = {"file": file, "path": path, "integrity": integrity}
        if size is not None:
            entry["size"] = size
        out.append(entry)
    if not any(o["file"] == "extension.js" for o in out):
        raise invalid
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _installed_view(r: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": r["name"], "version": r["version"], "title": r["title"], "icon": r.get("icon"), "id": r["id"],
        "installedAt": r["installedAt"], "installedBy": r["installedBy"], "hasConfig": bool(r.get("config")),
    }


def _config_values_of(config: Any) -> dict[str, Any]:
    """The ``config`` of an install or update: ``{ key: value }``; absent or ``None`` is none.  400 for anything else."""
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise DashboardMarketplaceError(400, "config must be an object")
    return dict(config)


def _check_config_values(config: Optional[DashboardPluginConfig], values: Mapping[str, Any]) -> None:
    """400 ``validation failed`` with per-key errors when values do not fit the settings."""
    errors = validate_dashboard_plugin_config_values(config, values)
    if errors:
        raise DashboardMarketplaceError(400, "validation failed", errors)


def _settings_of(name: str, target: Mapping[str, Any]) -> tuple[Optional[DashboardPluginConfig], Optional[str]]:
    """A version's settings; a bad record (a secret, invalid rules or defaults) installs without them, with a warning."""
    try:
        return parse_dashboard_plugin_config(target.get("config")), None
    except ValueError as exc:
        return None, f"the settings of {name}@{target['version']} are invalid and were ignored: {exc}"


def _record_config(record: Mapping[str, Any]) -> Optional[DashboardPluginConfig]:
    """The settings kept with an installed plugin (already checked when installed or loaded)."""
    try:
        return parse_dashboard_plugin_config(record.get("config"))
    except ValueError:
        return None


@dataclass
class _ServedPlugin:
    record: dict[str, Any]
    app: Any


class DashboardMarketplace:
    """Dashboard plugins from the marketplace (see the module's docstring)."""

    def __init__(self, options: DashboardMarketplaceOptions, deps: DashboardMarketplaceDeps) -> None:
        url = (options.url or DEFAULT_MARKETPLACE_URL).strip().rstrip("/")
        if not re.match(r"^https?://[^/\s]+", url, re.IGNORECASE):
            raise ValueError(f"invalid marketplace url: {options.url}")
        install = options.install or "verified"
        if install not in INSTALL_POLICIES:
            raise ValueError(f"invalid marketplace install policy: {install}")
        self.url = url
        self.install = install
        self.dir = os.path.abspath(options.dir or DEFAULT_DASHBOARD_PLUGINS_DIR)
        self._fetch: MarketplaceFetch = options.fetch or default_marketplace_fetch
        self._cdns = [c.rstrip("/") for c in (options.cdns or list(DEFAULT_DASHBOARD_PLUGIN_CDNS))]
        self._timeout = options.timeout
        self._deps = deps
        self._plugins: dict[str, _ServedPlugin] = {}
        #: The ``dashboard-plugins`` config provider: a section per installed plugin with settings (registered by
        #: the admin plugin).
        self.config = DashboardPluginsConfigProvider()
        # One lock per event loop (a test client may run each request on its own loop)
        self._locks: dict[int, asyncio.Lock] = {}

    def info(self) -> dict[str, Any]:
        """``GET /``: the marketplace, the install policy and the dashboard API this server runs."""
        return {"marketplaceUrl": self.url, "install": self.install, "dashboardApi": DASHBOARD_API_VERSION}

    def has_extension(self, extension_id: str) -> bool:
        """True when ``extension_id`` is the page of an installed marketplace plugin."""
        return extension_id in self._plugins

    # ------------------------------------------------------------------ browsing

    async def search(self, query: Optional[str] = None, page: Optional[int] = None, page_size: Optional[int] = None) -> dict[str, Any]:
        """``GET /plugins``: the marketplace's dashboard plugins, annotated.  502 when it is unreachable."""
        params: dict[str, str] = {"kind": "dashboard", "dashboardApi": str(DASHBOARD_API_VERSION)}
        if query and query.strip():
            params["query"] = query.strip()[:200]
        if page is not None:
            params["page"] = str(page)
        if page_size is not None:
            params["pageSize"] = str(page_size)
        body = await self._get_json(f"/plugins?{urlencode(params)}")
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise DashboardMarketplaceError(502, "marketplace unreachable")
        everything = body["items"]
        # Only dashboard plugins, whatever an older marketplace sends
        items = [
            i for i in everything
            if isinstance(i, dict) and is_valid_dashboard_plugin_name(i.get("name")) and i.get("kind") == "dashboard"
        ]
        # A marketplace that ignored the kind filter counted its other plugins too: its total only holds when it
        # sent nothing else
        filtered = len(items) == len(everything)
        total = body.get("total")
        return {
            "items": [{**item, **self._annotate(item)} for item in items],
            "total": total if filtered and isinstance(total, int) and not isinstance(total, bool) else len(items),
            "page": body["page"] if isinstance(body.get("page"), int) else (page if page is not None else 1),
            "pageSize": body["pageSize"] if isinstance(body.get("pageSize"), int) else (page_size if page_size is not None else len(items)),
        }

    async def details(self, name: str) -> dict[str, Any]:
        """``GET /plugins/{name}``: one plugin with its versions, annotated.  404 or 502."""
        plugin = await self._fetch_plugin(name)
        versions = []
        for v in plugin.get("versions") or []:
            if not isinstance(v, dict):
                continue
            reason = self._version_blocked_reason(plugin, v)
            versions.append({**v, "allowed": not reason, **({"blockedReason": reason} if reason else {})})
        return {**plugin, "versions": versions, **self._annotate(plugin)}

    def _annotate(self, item: Mapping[str, Any]) -> dict[str, Any]:
        installed = self._record_of(item.get("name"))
        if isinstance(item.get("versions"), list):
            fitting = _latest_fitting(item)
            latest = fitting.get("version") if fitting else None
            listed = _latest_listed(item)
            api = _api_of(fitting.get("dashboardApi")) if fitting else _api_of(listed.get("dashboardApi") if listed else None)
        else:
            latest = item.get("version") if item.get("version") is not None else item.get("latest")
            api = _api_of(item.get("dashboardApi"))
        blocked = self._blocked_reason(item, api)
        server_package = python_server_package(item.get("serverPackages"))
        out: dict[str, Any] = {
            "installed": {"version": installed["version"]} if installed else None,
            "updateAvailable": bool(
                installed and is_valid_dashboard_plugin_version(latest)
                and compare_dashboard_plugin_versions(latest, installed["version"]) > 0
            ),
            "allowed": not blocked,
        }
        if blocked:
            out["blockedReason"] = blocked
        out["requires"] = (
            {"serverPackage": server_package, "installed": server_package in self._deps.server_plugins()}
            if server_package else None
        )
        return out

    def _blocked_reason(self, item: Mapping[str, Any], api: int) -> Optional[str]:
        """Why the trust policy or the API refuses a plugin."""
        if self.install == "off":
            return "install disabled"
        publisher = item.get("publisher")
        if self.install == "verified" and not (isinstance(publisher, dict) and publisher.get("verified") is True):
            return "unverified publisher"
        if api > DASHBOARD_API_VERSION:
            return f"needs dashboard API {api}"
        return None

    def _version_blocked_reason(self, plugin: Mapping[str, Any], v: Mapping[str, Any]) -> Optional[str]:
        reason = self._blocked_reason(plugin, _api_of(v.get("dashboardApi")))
        if reason:
            return reason
        if not _is_dashboard_version(v, plugin):
            return "not a dashboard plugin"
        if v.get("status") != "listed":
            return f"version {v.get('status') or 'not listed'}"
        return None

    async def _get_json(self, path: str) -> Any:
        """GETs a path of the marketplace's public API: its JSON, ``None`` for a 404, 502 otherwise."""
        try:
            res = await self._fetch(f"{self.url}{MARKETPLACE_API_PATH}{path}", {"Accept": "application/json"}, self._timeout)
        except Exception:
            raise DashboardMarketplaceError(502, "marketplace unreachable") from None
        if res.status == 404:
            return None
        if not res.ok:
            raise DashboardMarketplaceError(502, "marketplace unreachable")
        try:
            return res.json()
        except Exception:
            raise DashboardMarketplaceError(502, "marketplace unreachable") from None

    async def _fetch_plugin(self, name: Any) -> dict[str, Any]:
        if not is_valid_dashboard_plugin_name(name):
            raise DashboardMarketplaceError(400, "invalid package name")
        plugin = await self._get_json(f"/plugins/{quote(name, safe='')}")
        if not isinstance(plugin, dict):
            raise DashboardMarketplaceError(404, f"plugin not found: {name}")
        if plugin.get("name") != name:
            raise DashboardMarketplaceError(502, "marketplace unreachable")
        return {**plugin, "versions": plugin["versions"] if isinstance(plugin.get("versions"), list) else []}

    # ------------------------------------------------------------------ installed

    def records(self) -> list[dict[str, Any]]:
        """The installed plugins, in installation order."""
        return [json.loads(json.dumps(p.record)) for p in self._plugins.values()]

    def _record_of(self, name: Any) -> Optional[dict[str, Any]]:
        return next((p.record for p in self._plugins.values() if p.record["name"] == name), None)

    async def installed(self, check_updates: bool = False) -> list[dict[str, Any]]:
        """``GET /installed``: the installed plugins; with ``check_updates``, ``updateAvailable`` from the
        marketplace (left out of a plugin the marketplace does not answer for)."""

        async def view(r: dict[str, Any]) -> dict[str, Any]:
            out = _installed_view(r)
            if not check_updates:
                return out
            try:
                latest = _latest_fitting(await self._fetch_plugin(r["name"]))
            except Exception:
                return out
            version = latest.get("version") if latest else None
            out["updateAvailable"] = bool(version) and compare_dashboard_plugin_versions(version, r["version"]) > 0
            return out

        return list(await asyncio.gather(*(view(r) for r in self.records())))

    # ------------------------------------------------------------------ changes

    def _lock(self) -> asyncio.Lock:
        loop = id(asyncio.get_running_loop())
        lock = self._locks.get(loop)
        if lock is None:
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    async def install_plugin(
        self, name: Any, version: Any, actor: DashboardMarketplaceActor, config: Any = None,
    ) -> dict[str, Any]:
        """``POST /installed``: installs ``name`` (the newest fitting version when ``version`` is absent) with the
        values of its settings in ``config`` (``{ key: value }``, checked against the version's settings before
        anything is downloaded: 400 ``validation failed`` with per-key ``errors``).  The installed plugin; ``warnings``
        when the marketplace's settings of the version are invalid (it is installed without settings)."""
        async with self._lock():
            if not is_valid_dashboard_plugin_name(name):
                raise DashboardMarketplaceError(400, "invalid package name")
            if version is not None and not is_valid_dashboard_plugin_version(version):
                raise DashboardMarketplaceError(400, "invalid version")
            values = _config_values_of(config)
            existing = self._record_of(name)
            if existing:
                raise DashboardMarketplaceError(409, f"already installed: {name}@{existing['version']}")
            plugin, target = await self._resolve(name, version)
            settings, warning = _settings_of(name, target)
            _check_config_values(settings, values)
            ext = target.get("extension") if isinstance(target.get("extension"), dict) else None
            ext = ext or (plugin.get("extension") if isinstance(plugin.get("extension"), dict) else None) or {}
            extension_id = dashboard_plugin_id(name, ext.get("id"))
            if not extension_id:
                raise DashboardMarketplaceError(400, f"no dashboard extension id for {name}")
            if extension_id in self._deps.extension_ids():
                raise DashboardMarketplaceError(409, f"dashboard extension already registered: {extension_id}")
            await self._config_ready()
            record = await self._store(name, target, extension_id, plugin, actor)
            if settings is not None:
                record["config"] = settings.to_dict()
            try:
                self._register(record)
            except ValueError as exc:
                shutil.rmtree(self._version_dir(name, target["version"]), ignore_errors=True)
                raise DashboardMarketplaceError(409, str(exc)) from None
            self._save()
            # A section left from an earlier install of the page (removed while its store was unreachable) starts clean
            leftover = list((await self.config.stored_values(extension_id)).keys())
            given = [k for k, v in values.items() if v is not None]
            changes: dict[str, Any] = {k: None for k in leftover if k not in given}
            changes.update({k: values[k] for k in given})
            await self._write_config(record, changes, actor)
            await self._changed("install", record, actor, extra={"config": given} if given else None)
            out = _installed_view(record)
            if warning:
                out["warnings"] = [warning]
            return out

    async def update_plugin(
        self, name: Any, version: Any, actor: DashboardMarketplaceActor, config: Any = None,
    ) -> dict[str, Any]:
        """``PUT /installed/{name}``: updates to ``version`` (default: the newest fitting one), with values of its
        settings in ``config`` (checked against the new version's settings before anything is downloaded).  Stored
        values whose key the new version still has and whose value it still accepts are kept, the others deleted
        (``configDropped``).  Returns ``{ plugin, changed, previousVersion, configDropped, warnings? }``; ``changed``
        is false when it already is that version (the given values are applied all the same)."""
        async with self._lock():
            if not is_valid_dashboard_plugin_name(name):
                raise DashboardMarketplaceError(400, "invalid package name")
            if version is not None and not is_valid_dashboard_plugin_version(version):
                raise DashboardMarketplaceError(400, "invalid version")
            values = _config_values_of(config)
            current = self._record_of(name)
            if not current:
                raise DashboardMarketplaceError(404, f"not installed: {name}")
            plugin, target = await self._resolve(name, version)
            await self._config_ready()
            if target["version"] == current["version"]:
                _check_config_values(_record_config(current), values)
                await self._write_config(current, values, actor)
                return {
                    "plugin": _installed_view(current), "changed": False, "previousVersion": current["version"],
                    "configDropped": [],
                }
            settings, warning = _settings_of(name, target)
            _check_config_values(settings, values)
            # The page keeps its id, so open dashboards and links keep working
            record = await self._store(name, target, current["id"], plugin, actor)
            if settings is not None:
                record["config"] = settings.to_dict()
            self._unregister(current["id"])
            try:
                self._register(record)
            except ValueError as exc:
                self._register(current)
                shutil.rmtree(self._version_dir(name, target["version"]), ignore_errors=True)
                raise DashboardMarketplaceError(409, str(exc)) from None
            self._save()
            shutil.rmtree(self._version_dir(name, current["version"]), ignore_errors=True)
            # Stored values the new version still accepts are kept, the others dropped
            stored = await self.config.stored_values(record["id"])
            given = [k for k, v in values.items() if v is not None]
            dropped = [
                key for key, value in stored.items()
                if (entry := settings.entry(key) if settings is not None else None) is None
                or validate_cli_config_value(entry, value) is not None
            ]
            changes: dict[str, Any] = {k: None for k in dropped}
            changes.update(values)
            await self._write_config(record, changes, actor)
            config_dropped = [k for k in dropped if k not in given]
            extra: dict[str, Any] = {}
            if given:
                extra["config"] = given
            if config_dropped:
                extra["configDropped"] = config_dropped
            await self._changed("update", record, actor, current["version"], extra or None)
            out: dict[str, Any] = {
                "plugin": _installed_view(record), "changed": True, "previousVersion": current["version"],
                "configDropped": config_dropped,
            }
            if warning:
                out["warnings"] = [warning]
            return out

    async def remove_plugin(self, name: Any, actor: DashboardMarketplaceActor) -> dict[str, Any]:
        """``DELETE /installed/{name}``: unregisters the page, deletes its files and its settings."""
        async with self._lock():
            if not is_valid_dashboard_plugin_name(name):
                raise DashboardMarketplaceError(400, "invalid package name")
            current = self._record_of(name)
            if not current:
                raise DashboardMarketplaceError(404, f"not installed: {name}")
            self._unregister(current["id"])
            self._save()
            shutil.rmtree(os.path.join(self.dir, dashboard_plugin_dir_name(name)), ignore_errors=True)
            # Its settings go with it
            await self._config_ready()
            keys, config_version = await self.config.clear(current["id"])
            if config_version is not None:
                await self._config_changed(current["id"], keys, config_version, actor)
            await self._changed("remove", current, actor)
            return _installed_view(current)

    async def plugin_config(self, name: Any) -> dict[str, Any]:
        """``GET /installed/{name}/config``: the values of a plugin's settings (stored over defaults) and the
        section's version; ``{ values: {}, version: None }`` for a plugin without settings.  404 when it is not
        installed."""
        if not is_valid_dashboard_plugin_name(name):
            raise DashboardMarketplaceError(400, "invalid package name")
        record = self._record_of(name)
        if not record:
            raise DashboardMarketplaceError(404, f"not installed: {name}")
        await self._config_ready()
        return (await self.config.values(record["id"])) or {"values": {}, "version": None}

    async def plugin_info(self, name: Any) -> dict[str, Any]:
        """An installed plugin with its files and its settings' values (``admin dashboard info``): ``{ plugin, files,
        configVersion, settings }``.  404 when it is not installed."""
        if not is_valid_dashboard_plugin_name(name):
            raise DashboardMarketplaceError(400, "invalid package name")
        record = self._record_of(name)
        if not record:
            raise DashboardMarketplaceError(404, f"not installed: {name}")
        await self._config_ready()
        current = await self.config.values(record["id"])
        stored = await self.config.stored_values(record["id"]) if current else {}
        settings = _record_config(record)
        return {
            "plugin": _installed_view(record),
            "files": [dict(f) for f in record.get("files") or []],
            "configVersion": current["version"] if current else None,
            "settings": [
                {**e.to_dict(), "value": current["values"].get(e.key) if current else e.default, "isDefault": e.key not in stored}
                for e in (settings.entries if settings is not None else [])
            ],
        }

    async def _config_ready(self) -> None:
        if self._deps.config_ready is not None:
            await self._deps.config_ready()

    async def _write_config(self, record: Mapping[str, Any], changes: Mapping[str, Any], actor: DashboardMarketplaceActor) -> list[str]:
        """Writes a plugin's setting changes (none: nothing written) and publishes ``config:changed``; the keys written."""
        keys = list(changes.keys())
        if not keys:
            return keys
        config_version = await self.config.write(record["id"], changes)
        await self._config_changed(record["id"], keys, config_version, actor)
        return keys

    async def _config_changed(self, section: str, keys: list[str], version: str, actor: DashboardMarketplaceActor) -> None:
        if self._deps.publish_config_changed is None:
            return
        try:
            await self._deps.publish_config_changed({
                "provider": DASHBOARD_PLUGINS_CONFIG_PROVIDER_ID, "section": section, "keys": keys, "version": version,
                "requiresRestart": False, "store": self.config.store_kind, "user": actor.user_name,
            })
        except Exception as exc:  # noqa: BLE001 - the change is made; the event failing is logged
            logger.error("publishing config:changed failed: %s", exc)

    async def _resolve(self, name: str, version: Optional[str]) -> tuple[dict[str, Any], dict[str, Any]]:
        """The plugin and the version to install, after the kind, trust policy and API checks."""
        plugin = await self._fetch_plugin(name)
        if plugin.get("kind") != "dashboard":
            raise DashboardMarketplaceError(400, f"not a dashboard plugin: {name}")
        if self.install == "off":
            raise DashboardMarketplaceError(403, "install disabled")
        publisher = plugin.get("publisher")
        if self.install == "verified" and not (isinstance(publisher, dict) and publisher.get("verified") is True):
            raise DashboardMarketplaceError(403, "publisher not verified")
        target: Optional[dict[str, Any]]
        if version:
            target = next((v for v in plugin["versions"] if isinstance(v, dict) and v.get("version") == version), None)
            if not target or target.get("status") != "listed":
                raise DashboardMarketplaceError(404, f"version not found: {name}@{version}")
            if not _is_dashboard_version(target, plugin):
                raise DashboardMarketplaceError(400, f"not a dashboard plugin: {name}@{version}")
            api = _api_of(target.get("dashboardApi"))
            if api > DASHBOARD_API_VERSION:
                raise DashboardMarketplaceError(400, f"needs dashboard API {api}")
        else:
            target = _latest_fitting(plugin)
            if not target:
                newest = _latest_listed(plugin)
                if newest:
                    raise DashboardMarketplaceError(400, f"needs dashboard API {_api_of(newest.get('dashboardApi'))}")
                raise DashboardMarketplaceError(404, f"no installable version: {name}")
        if not isinstance(target.get("files"), list) or "config" not in target:
            # A details view without files (an older marketplace) or settings: the version record has them
            record = await self._get_json(f"/plugins/{quote(name, safe='')}/versions/{quote(target['version'], safe='')}")
            if not isinstance(record, dict) and not isinstance(target.get("files"), list):
                raise DashboardMarketplaceError(404, f"version not found: {name}@{target['version']}")
            record = record if isinstance(record, dict) else {}
            target = {
                **target,
                "files": target["files"] if isinstance(target.get("files"), list) else record.get("files"),
                "config": target["config"] if target.get("config") is not None else record.get("config"),
            }
        return plugin, target

    async def _store(
        self, name: str, target: Mapping[str, Any], extension_id: str, plugin: Mapping[str, Any], actor: DashboardMarketplaceActor,
    ) -> dict[str, Any]:
        """Downloads and verifies every file, then stores them in ``<dir>/<name>/<version>/`` (staged, then
        renamed into place)."""
        planned = _planned_files(target.get("files"))
        downloaded: list[tuple[dict[str, Any], bytes]] = []
        for f in planned:
            data = await self._download(name, target["version"], f["path"], f["integrity"])
            downloaded.append(({"file": f["file"], "path": f["path"], "integrity": f["integrity"], "size": len(data)}, data))
        # Verified: now write
        os.makedirs(self.dir, exist_ok=True)
        staging = os.path.join(self.dir, f".staging-{secrets.token_hex(6)}")
        final = self._version_dir(name, target["version"])
        try:
            os.mkdir(staging)
            for info, data in downloaded:
                with open(os.path.join(staging, info["file"]), "wb") as out:
                    out.write(data)
            os.makedirs(os.path.join(self.dir, dashboard_plugin_dir_name(name)), exist_ok=True)
            shutil.rmtree(final, ignore_errors=True)
            os.rename(staging, final)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        title = next(
            (t for t in (plugin.get("title"), plugin.get("shortName"), name) if isinstance(t, str) and t.strip()), name,
        )
        record: dict[str, Any] = {"name": name, "version": target["version"], "id": extension_id, "title": title.strip()[:40]}
        if isinstance(plugin.get("icon"), str) and plugin["icon"]:
            record["icon"] = plugin["icon"]
        record["files"] = [info for info, _ in downloaded]
        record["installedAt"] = _now_iso()
        record["installedBy"] = actor.user_name
        return record

    async def _download(self, name: str, version: str, path: str, integrity: str) -> bytes:
        """One file from the first CDN whose bytes match ``integrity``; 502 ``integrity mismatch`` or
        ``download failed``."""
        encoded_path = "/".join(quote(s, safe="") for s in path.split("/"))
        mismatch = False
        for cdn in self._cdns:
            url = f"{cdn}/{name}@{quote(version, safe='')}/{encoded_path}"
            try:
                res = await self._fetch(url, {}, self._timeout)
            except Exception:
                continue
            if not res.ok:
                continue
            length = res.headers.get("content-length") if res.headers else None
            if length is not None and str(length).isdigit() and int(length) > DASHBOARD_PLUGIN_MAX_FILE_BYTES:
                continue
            data = res.body
            if len(data) > DASHBOARD_PLUGIN_MAX_FILE_BYTES:
                continue
            if sha384_integrity(data) == integrity:
                return data
            mismatch = True
        raise DashboardMarketplaceError(502, f"integrity mismatch: {path}" if mismatch else f"download failed: {path}")

    def _version_dir(self, name: str, version: str) -> str:
        path = os.path.abspath(os.path.join(self.dir, dashboard_plugin_dir_name(name), version))
        # Valid names and versions never leave the directory; checked all the same
        if not path.startswith(self.dir + os.sep) or not is_valid_dashboard_plugin_version(version):
            raise DashboardMarketplaceError(400, "invalid version")
        return path

    def _register(self, record: dict[str, Any]) -> None:
        """Adds the plugin's page and serves its files.  Raises ``ValueError`` when the id is taken."""
        extension: dict[str, Any] = {"id": record["id"], "title": record["title"], "version": record["version"]}
        if record.get("icon"):
            extension["icon"] = record["icon"]
        page = dashboard_module_page(
            self._version_dir(record["name"], record["version"]), f"{DASHBOARD_MODULES_PREFIX}/{record['id']}", extension,
        )
        try:
            self._deps.add_extension(page.extension)
        except ValueError as exc:
            if "icon" not in page.extension or "already registered" in str(exc):
                raise
            # An icon the dashboard cannot show is left out
            self._deps.add_extension({k: v for k, v in page.extension.items() if k != "icon"})
        self._plugins[record["id"]] = _ServedPlugin(record=record, app=page.app)
        settings = _record_config(record)
        if settings is not None:
            self.config.set_section(record["id"], record["title"], settings, f"{record['name']}@{record['version']}")
        else:
            self.config.remove_section(record["id"])

    def _unregister(self, extension_id: str) -> None:
        self._deps.remove_extension(extension_id)
        self._plugins.pop(extension_id, None)
        self.config.remove_section(extension_id)

    def _save(self) -> None:
        """Writes ``installed.json`` atomically (a temporary file renamed over it)."""
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, INSTALLED_FILE)
        temp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        body = json.dumps({"plugins": [p.record for p in self._plugins.values()]}, indent=2, ensure_ascii=False) + "\n"
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(body)
            os.replace(temp, path)
        except BaseException:
            try:
                os.remove(temp)
            except OSError:
                pass
            raise

    async def _changed(
        self, action: str, record: Mapping[str, Any], actor: DashboardMarketplaceActor, previous: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
        event: dict[str, Any] = {"name": record["name"], "version": record["version"], "id": record["id"]}
        if previous:
            event["previousVersion"] = previous
        properties = {**event, **(extra or {})}
        sink = self._deps.audit()
        if sink is not None:
            try:
                result = sink(AuditEntry(
                    timestamp=int(time.time() * 1000),
                    event=f"dashboard.plugin.{action}",
                    username=actor.user_name,
                    user_id=actor.user_id,
                    actor=actor.user_name,
                    actor_id=actor.user_id,
                    target=record["name"],
                    details=f"{record['name']} {previous} -> {record['version']}" if previous else f"{record['name']}@{record['version']}",
                    properties=properties,
                ))
                if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                    await result
            except Exception as exc:  # noqa: BLE001 - the change is made; the audit failing is logged
                logger.error("audit of a dashboard plugin change failed: %s", exc)
        if self._deps.publish is not None:
            data = {"action": action, **event}
            try:
                await self._deps.publish(DASHBOARD_EXTENSIONS_CHANGED_EVENT, data)
            except Exception as exc:  # noqa: BLE001
                logger.error("publishing %s failed: %s", DASHBOARD_EXTENSIONS_CHANGED_EVENT, exc)

    # ------------------------------------------------------------------ startup and serving

    def load(self) -> None:
        """Registers the plugins ``installed.json`` lists (at startup).  A plugin whose files are missing or changed
        on disk, or whose id is taken, is skipped with a warning; a broken ``installed.json`` loads nothing."""
        path = os.path.join(self.dir, INSTALLED_FILE)
        if not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                records = json.load(f).get("plugins")
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s is not readable JSON, no dashboard plugins loaded: %s", path, exc)
            return
        if not isinstance(records, list):
            return
        for raw in records:
            raw = raw if isinstance(raw, dict) else {}
            label = f"{raw.get('name')}@{raw.get('version')}"
            try:
                name, version, extension_id = raw.get("name"), raw.get("version"), raw.get("id")
                if (
                    not is_valid_dashboard_plugin_name(name) or not is_valid_dashboard_plugin_version(version)
                    or not isinstance(extension_id, str) or not _EXTENSION_ID.fullmatch(extension_id)
                ):
                    raise ValueError("invalid record")
                if self._record_of(name):
                    raise ValueError("listed twice")
                raw_files = raw.get("files") if isinstance(raw.get("files"), list) else None
                files = _planned_files(
                    [{"path": f.get("path"), "integrity": f.get("integrity")} if isinstance(f, dict) else None for f in raw_files]
                    if raw_files is not None else None
                )
                directory = self._version_dir(name, version)
                for f in files:
                    file_path = os.path.join(directory, f["file"])
                    if not os.path.isfile(file_path):
                        raise ValueError(f"{f['file']} is missing or changed on disk")
                    with open(file_path, "rb") as fh:
                        if sha384_integrity(fh.read()) != f["integrity"]:
                            raise ValueError(f"{f['file']} is missing or changed on disk")
                if extension_id in self._deps.extension_ids():
                    raise ValueError(f"dashboard extension already registered: {extension_id}")
                title = raw.get("title")
                record: dict[str, Any] = {
                    "name": name, "version": version, "id": extension_id,
                    "title": title[:40] if isinstance(title, str) and title.strip() else name[:40],
                }
                if isinstance(raw.get("icon"), str):
                    record["icon"] = raw["icon"]
                record["files"] = [
                    {"file": f.get("file"), "path": f.get("path"), "integrity": f.get("integrity"), "size": f.get("size")}
                    for f in raw_files or []
                ]
                record["installedAt"] = raw["installedAt"] if isinstance(raw.get("installedAt"), str) else ""
                record["installedBy"] = raw["installedBy"] if isinstance(raw.get("installedBy"), str) else ""
                try:
                    settings = parse_dashboard_plugin_config(raw.get("config"))
                    if settings is not None:
                        record["config"] = settings.to_dict()
                except ValueError as exc:
                    logger.warning("the settings of dashboard plugin %s are invalid, loaded without them: %s", label, exc)
                self._register(record)
            except (ValueError, DashboardMarketplaceError, OSError) as exc:
                logger.warning("dashboard plugin %s not loaded: %s", label, exc)

    def handles(self, path: str) -> bool:
        """Whether ``path`` (below ``/qcli/modules``) is a file of an installed plugin (``/<id>/...``)."""
        match = _SERVED.fullmatch(path)
        return bool(match and match.group(1) in self._plugins)

    async def app(self, scope: dict, receive: Any, send: Any) -> None:
        """ASGI app serving the installed plugins' files below ``/qcli/modules`` (``/<id>/extension.js``,
        ``/<id>/extension.css``), mounted by the server through the plugin's ``asset_routes``."""
        root = scope.get("root_path", "")
        path = scope.get("path", "")
        below = path[len(root):] if root and path.startswith(root) else path
        match = _SERVED.fullmatch(below)
        plugin = self._plugins.get(match.group(1)) if match else None
        if plugin is None:
            from starlette.responses import PlainTextResponse

            await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
            return
        await plugin.app({**scope, "root_path": f"{root}/{match.group(1)}"}, receive, send)


@dataclass
class DashboardMarketplaceAssetRoute:
    """The marketplace's files at ``/qcli/modules`` (a mounted plugin's ``asset_routes`` entry)."""

    marketplace: DashboardMarketplace
    prefix: str = DASHBOARD_MODULES_PREFIX

    @property
    def app(self) -> Any:
        return self.marketplace.app

    def handles(self, path: str) -> bool:
        return self.marketplace.handles(path)
