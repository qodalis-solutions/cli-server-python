"""Server plugins from the marketplace (``kind=server``), the Python package only (``platforms.python``): browse them,
install, update and remove them at runtime.  An install downloads the wheel PyPI lists for the version, checks its
sha256 against PyPI's and the marketplace's, checks the wheel itself (its name, version, ``qodalis_cli.plugins`` entry
point and the server range of its ``Requires-Dist``), then runs ``pip install --target <dir> --no-deps`` on it in a
staging directory, with the dependencies the server's environment lacks (the server's own packages never), and moves
the result into ``<data dir>/server-plugins``.  The plugin is recorded in ``installed.json`` and a restart is marked
as required: the loader runs it at the next start.  The same contract and JSON as the Node server."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.metadata as metadata
import io
import logging
import os
import re
import secrets
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.parser import Parser
from typing import Any, Awaitable, Callable, Mapping, Optional
from urllib.parse import quote, urlencode

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from qodalis_cli_server_abstractions.audit import AuditEntry
from qodalis_cli_server_abstractions.plugin_entry import (
    CLI_PLUGINS_INSTALL_PERMISSION,
    CLI_SERVER_PLUGIN_ENTRY_GROUP,
    CLI_SERVER_PLUGIN_KIND,
)

from .dashboard_marketplace import (
    DEFAULT_MARKETPLACE_URL,
    INSTALL_POLICIES,
    MARKETPLACE_API_PATH,
    DashboardMarketplaceActor,
    DashboardMarketplaceError,
    MarketplaceFetch,
    MarketplaceResponse,
    default_marketplace_fetch,
)

logger = logging.getLogger("qcli-admin")

#: This server's platform in the marketplace (``platforms.python``).
SERVER_PLUGIN_PLATFORM = "python"
#: The platforms a server plugin may have a package for.
SERVER_PLUGIN_PLATFORMS = ("node", "python", "dotnet")
PLUGINS_INSTALL_PERMISSION = CLI_PLUGINS_INSTALL_PERMISSION
#: The package index wheels and their hashes come from.
DEFAULT_PYPI_URL = "https://pypi.org"
#: The largest wheel accepted.
SERVER_PLUGIN_MAX_WHEEL_BYTES = 50 * 1024 * 1024
#: The server package whose version this server reports (every server package shares one version).
SERVER_CORE_PACKAGE = "qodalis-cli-server"
#: The packages a plugin's server range may be on (the core, then the abstractions); never installed with a plugin.
SERVER_CORE_PACKAGES = ("qodalis-cli-server", "qodalis-cli-server-abstractions")
#: Published when a server plugin is installed, updated or removed (audience ``admin:access``).
SERVER_PLUGINS_CHANGED_EVENT = "server:plugins-changed"

_PLUGIN_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_PYPI_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?")
_SHA256 = re.compile(r"sha256-[A-Za-z0-9+/]{43}=")


@dataclass
class PipRunResult:
    """What pip printed and how it ended."""

    code: int
    stdout: str = ""
    stderr: str = ""


#: Runs ``python -m pip`` with these arguments (tests pass a fake: no pip, no network).
PipRunner = Callable[[list[str]], Awaitable[PipRunResult]]


async def default_pip_runner(args: list[str]) -> PipRunResult:
    """Runs this interpreter's pip (``sys.executable -m pip``), 5 minutes at most."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=300)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return PipRunResult(1, "", "pip timed out after 5 minutes")
    return PipRunResult(process.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


async def default_wheel_fetch(url: str, headers: Mapping[str, str], timeout: float) -> MarketplaceResponse:
    """GETs a wheel with the standard library (in a thread); reads at most 50 MB and a byte."""

    def get() -> MarketplaceResponse:
        request = urllib.request.Request(url, headers={"User-Agent": "qodalis-cli-server-python", **headers})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as res:  # noqa: S310 - https URLs from PyPI
                return MarketplaceResponse(
                    res.status, res.read(SERVER_PLUGIN_MAX_WHEEL_BYTES + 1), {k.lower(): v for k, v in res.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            return MarketplaceResponse(exc.code, b"", {})

    return await asyncio.to_thread(get)


@dataclass
class ServerPluginMarketplaceOptions:
    """The server marketplace's options (from the admin plugin's ``set_marketplace``, shared with dashboard plugins)."""

    url: Optional[str] = None
    install: Optional[str] = None
    #: The HTTP client of the marketplace and PyPI's JSON API (default: the standard library's).
    fetch: Optional[MarketplaceFetch] = None
    #: The HTTP client of wheel downloads (default: ``fetch`` when given, else the standard library's, 50 MB).
    download: Optional[MarketplaceFetch] = None
    timeout: float = 15.0
    #: The package index (default ``https://pypi.org``; its JSON API at ``/pypi/<name>/<version>/json``).
    pypi_url: Optional[str] = None
    #: Runs pip (default: ``sys.executable -m pip``).
    pip: Optional[PipRunner] = None
    #: The server's version (default: the installed ``qodalis-cli-server``'s).
    server_version: Optional[str] = None
    #: Dependencies a plugin may bring when the marketplace pins none (names, or prefixes ``acme-*``); without a pin
    #: or a match the install is refused ("untrusted dependency").
    trusted_dependencies: Optional[list[str]] = None


@dataclass
class ServerPluginMarketplaceDeps:
    #: The server's runtime plugins (``builder.server_plugins``): the installed list and what this process loaded.
    plugins: Any
    #: Every installed package (the host's and the marketplace's), for clashes.
    installed_plugins: Callable[[], list[Any]]
    #: The server's restart service: every change is marked as needing a restart.
    restart: Any = None
    audit: Callable[[], Any] = lambda: None
    publish: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]] = None


def is_valid_server_plugin_name(name: Any) -> bool:
    """True for a marketplace plugin id (``jobs``) or a PyPI name."""
    return isinstance(name, str) and len(name) <= 100 and (
        _PLUGIN_ID.fullmatch(name) is not None or _PYPI_NAME.fullmatch(name) is not None
    )


def _is_package(name: Any) -> bool:
    return isinstance(name, str) and _PYPI_NAME.fullmatch(name) is not None


def _version(text: Any) -> Optional[Version]:
    if not isinstance(text, str) or len(text) > 64:
        return None
    try:
        return Version(text)
    except InvalidVersion:
        return None


def _is_version(text: Any) -> bool:
    return _version(text) is not None


def compare_server_plugin_versions(a: str, b: str) -> int:
    """PEP 440 order (``1.0.0b31`` < ``1.0.0``); text order for anything else."""
    va, vb = _version(a), _version(b)
    if va is None or vb is None:
        return -1 if a < b else 1 if a > b else 0
    return -1 if va < vb else 1 if va > vb else 0


def satisfies_server_range(server: str, spec: Any) -> bool:
    """Whether ``server`` (``1.0.0b31``) satisfies a PEP 440 specifier (``>=1.0.0b7``), prereleases included; no range,
    ``*`` or an unreadable one: true."""
    if not isinstance(spec, str) or not spec.strip() or spec.strip() == "*":
        return True
    version = _version(server)
    if version is None:
        return True
    try:
        return SpecifierSet(spec.strip(), prereleases=True).contains(version, prereleases=True)
    except InvalidSpecifier:
        return True


def sha256_integrity(data: bytes) -> str:
    """``sha256-<base64>`` of the bytes (the marketplace's form of PyPI's sha256)."""
    return "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _hex_to_integrity(hex_digest: Any) -> Optional[str]:
    if not isinstance(hex_digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", hex_digest):
        return None
    return "sha256-" + base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------- marketplace records


def _platforms_of(item: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The platforms of an item (``{ python: {...} }``) from either form (a map or a list with ``platform``)."""
    out: dict[str, dict[str, Any]] = {}
    raw = item.get("platforms")
    if isinstance(raw, list):
        for p in raw:
            if isinstance(p, dict) and isinstance(p.get("platform"), str) and isinstance(p.get("package"), str):
                out[p["platform"]] = p
    elif isinstance(raw, dict):
        for key, p in raw.items():
            if isinstance(p, dict) and isinstance(p.get("package"), str):
                out[key] = p
    return out


def _platform_versions(item: Mapping[str, Any], p: Mapping[str, Any]) -> Optional[list[dict[str, Any]]]:
    """This platform's versions: the plugin's flat list (each with its ``platform``), else the platform's own."""
    if isinstance(item.get("versions"), list):
        return [v for v in item["versions"] if isinstance(v, dict) and v.get("platform", SERVER_PLUGIN_PLATFORM) == SERVER_PLUGIN_PLATFORM]
    return [v for v in p["versions"] if isinstance(v, dict)] if isinstance(p.get("versions"), list) else None


def _listed_versions(item: Mapping[str, Any], p: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The listed versions of this platform, newest first (its ``latest`` alone for a list item)."""
    versions = _platform_versions(item, p)
    if versions is None:
        versions = [{"version": p["latest"], "status": "listed", "serverRange": p.get("serverRange")}] if p.get("latest") else []
    listed = [v for v in versions if _is_version(v.get("version")) and v.get("status", "listed") == "listed"]
    listed.sort(key=lambda v: _version(v["version"]), reverse=True)  # type: ignore[arg-type, return-value]
    return listed


def _auto_install_of(item: Mapping[str, Any], p: Optional[Mapping[str, Any]], v: Optional[Mapping[str, Any]] = None) -> tuple[bool, Optional[str]]:
    """Whether the plugin installs at runtime: the version's, else the platform's, else the item's ``autoInstall``."""
    for source in (v, p, item):
        if source is not None and isinstance(source.get("autoInstall"), bool):
            if source["autoInstall"]:
                return True, None
            reason = source.get("autoInstallReason")
            return False, reason if isinstance(reason, str) else None
    return True, None


def _needs_configuration_of(item: Mapping[str, Any], p: Optional[Mapping[str, Any]], v: Optional[Mapping[str, Any]] = None) -> list[str]:
    for source in (v, p, item):
        if source is not None and isinstance(source.get("needsConfiguration"), list):
            return [k for k in source["needsConfiguration"] if isinstance(k, str)]
    return []


# ---------------------------------------------------------------------- wheels


def _choose_wheel(files: list[Any]) -> Optional[dict[str, Any]]:
    """The wheel pip would take here: a pure-Python one, else the most specific compatible wheel; ``None`` if none."""
    tags = {str(t): i for i, t in enumerate(sys_tags())}
    best: Optional[tuple[int, dict[str, Any]]] = None
    for f in files:
        if not isinstance(f, dict) or f.get("packagetype") != "bdist_wheel" or f.get("yanked") is True:
            continue
        filename = f.get("filename")
        try:
            _, _, _, wheel_tags = parse_wheel_filename(filename)
        except (InvalidWheelFilename, TypeError):
            continue
        ranks = [tags[str(t)] for t in wheel_tags if str(t) in tags]
        if not ranks:
            continue
        pure = any(t.abi == "none" and t.platform == "any" for t in wheel_tags)
        rank = -1 if pure else min(ranks)
        if best is None or rank < best[0]:
            best = (rank, f)
    return best[1] if best else None


@dataclass
class WheelInfo:
    name: str
    version: str
    summary: Optional[str]
    requires: list[str]
    entry_points: list[str]


def read_wheel(data: bytes) -> WheelInfo:
    """The name, version, summary, ``Requires-Dist`` and ``qodalis_cli.plugins`` entries of a wheel."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DashboardMarketplaceError(502, "invalid wheel") from exc
    names = archive.namelist()
    meta = next((n for n in names if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)), None)
    if meta is None:
        raise DashboardMarketplaceError(502, "invalid wheel: no .dist-info/METADATA")
    for n in names:
        if n.startswith("/") or ".." in n.split("/"):
            raise DashboardMarketplaceError(502, "invalid wheel: a path outside the package")
    message = Parser().parsestr(archive.read(meta).decode("utf-8", "replace"))
    entries: list[str] = []
    ep_path = meta.rsplit("/", 1)[0] + "/entry_points.txt"
    if ep_path in names:
        section = None
        for line in archive.read(ep_path).decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
            elif section == CLI_SERVER_PLUGIN_ENTRY_GROUP and "=" in line:
                entries.append(line.split("=", 1)[0].strip())
    return WheelInfo(
        name=str(message.get("Name") or ""),
        version=str(message.get("Version") or ""),
        summary=message.get("Summary"),
        requires=[str(r) for r in message.get_all("Requires-Dist") or []],
        entry_points=entries,
    )


def _host_version(name: str, exclude: str) -> Optional[str]:
    """A distribution's version in the server's environment (outside ``exclude``), else ``None``."""
    paths = [p for p in sys.path if os.path.abspath(p or ".") != exclude]
    dist = next(iter(metadata.distributions(name=name, path=paths)), None)
    return dist.version if dist is not None else None


def _dir_version(name: str, directory: str) -> Optional[str]:
    if not os.path.isdir(directory):
        return None
    dist = next(iter(metadata.distributions(name=name, path=[directory])), None)
    return dist.version if dist is not None else None


def missing_requirements(requires: list[str], plugins_dir: str) -> list[str]:
    """The ``Requires-Dist`` this environment does not satisfy: markers evaluated (extras left out), the server's own
    packages never, a requirement met by the server's environment or the plugins directory skipped."""
    out: list[str] = []
    for text in requires:
        try:
            req = Requirement(text)
        except InvalidRequirement:
            continue
        if req.marker is not None and not req.marker.evaluate({"extra": ""}):
            continue
        name = canonicalize_name(req.name)
        if name in SERVER_CORE_PACKAGES:
            continue
        host = _host_version(req.name, plugins_dir)
        if host is not None:
            # Never installed beside (or shadowing) the server's own copy
            if req.specifier and not req.specifier.contains(host, prereleases=True):
                raise DashboardMarketplaceError(400, f"incompatible dependency: needs {req.name}{req.specifier}, the server has {host}")
            continue
        have = _dir_version(req.name, plugins_dir)
        if have is not None and (not req.specifier or req.specifier.contains(have, prereleases=True)):
            continue
        out.append(str(req))
    return out


def is_trusted_dependency(name: str, trusted: list[str]) -> bool:
    """Whether the host's ``trusted_dependencies`` allows a package: a name (``httpx``) or a prefix (``qodalis-*``),
    compared normalized."""
    wanted = canonicalize_name(name)
    for pattern in trusted:
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        text = pattern.strip()
        if text.endswith("*"):
            if wanted.startswith(canonicalize_name(text[:-1]) if text[:-1] else ""):
                return True
        elif canonicalize_name(text) == wanted:
            return True
    return False


def _integrity_hex(integrity: Any) -> Optional[str]:
    """``sha256-<base64>`` -> its hex digest; ``None`` for anything else."""
    if not isinstance(integrity, str) or not _SHA256.fullmatch(integrity):
        return None
    try:
        return base64.b64decode(integrity[len("sha256-"):]).hex()
    except ValueError:
        return None


def pinned_dependencies(target: Any) -> Optional[list[dict[str, str]]]:
    """The dependencies the marketplace pinned for a version (``dependencies: [{ id, version, integrity, range }]``,
    sha256 integrity), or ``None`` when it pins none.  Raises 502 for a malformed pin."""
    raw = target.get("dependencies") if isinstance(target, Mapping) else None
    if not isinstance(raw, list) or not raw:
        return None
    pins: list[dict[str, str]] = []
    for d in raw:
        if not isinstance(d, Mapping) or not _is_package(d.get("id")) or not _is_version(d.get("version")):
            raise DashboardMarketplaceError(502, "invalid marketplace record: dependencies")
        digest = _integrity_hex(d.get("integrity"))
        if digest is None:
            raise DashboardMarketplaceError(502, f"invalid marketplace record: no sha256 for the dependency {d.get('id')}")
        pins.append({"id": str(d["id"]), "version": str(d["version"]), "sha256": digest})
    return pins


def _dist_info_dirs(directory: str, package: str) -> list[str]:
    """The ``*.dist-info`` directories of ``package`` in ``directory``."""
    wanted = canonicalize_name(package)
    out = []
    if not os.path.isdir(directory):
        return out
    for entry in os.listdir(directory):
        if entry.endswith(".dist-info"):
            name = entry[: -len(".dist-info")].rsplit("-", 1)[0]
            if canonicalize_name(name) == wanted:
                out.append(os.path.join(directory, entry))
    return out


def uninstall_from(directory: str, package: str) -> list[str]:
    """Removes a distribution installed with ``pip install --target``: the files its ``RECORD`` lists (inside
    ``directory`` only), the directories left empty and its ``.dist-info``.  Its dependencies stay."""
    removed: list[str] = []
    root = os.path.realpath(directory)
    for info in _dist_info_dirs(directory, package):
        record = os.path.join(info, "RECORD")
        dirs: set[str] = set()
        if os.path.isfile(record):
            with open(record, encoding="utf-8") as f:
                for line in f:
                    rel = line.split(",", 1)[0].strip()
                    if not rel:
                        continue
                    path = os.path.realpath(os.path.join(directory, rel))
                    if not path.startswith(root + os.sep):
                        continue
                    if os.path.isfile(path) or os.path.islink(path):
                        os.remove(path)
                        removed.append(rel)
                    parent = os.path.dirname(path)
                    while parent.startswith(root + os.sep):
                        dirs.add(parent)
                        parent = os.path.dirname(parent)
        for d in sorted(dirs, key=len, reverse=True):
            if os.path.isdir(d) and d.endswith("__pycache__"):
                shutil.rmtree(d, ignore_errors=True)
            try:
                os.rmdir(d)
            except OSError:
                pass
        shutil.rmtree(info, ignore_errors=True)
    return removed


# ---------------------------------------------------------------------- the service


class ServerPluginMarketplace:
    """Server plugins from the marketplace (see the module's docstring)."""

    def __init__(self, options: ServerPluginMarketplaceOptions, deps: ServerPluginMarketplaceDeps) -> None:
        url = (options.url or DEFAULT_MARKETPLACE_URL).strip().rstrip("/")
        if not re.match(r"^https?://[^/\s]+", url, re.IGNORECASE):
            raise ValueError(f"invalid marketplace url: {options.url}")
        install = options.install or "verified"
        if install not in INSTALL_POLICIES:
            raise ValueError(f"invalid marketplace install policy: {install}")
        pypi = (options.pypi_url or DEFAULT_PYPI_URL).strip().rstrip("/")
        if not re.match(r"^https?://[^/\s]+", pypi, re.IGNORECASE):
            raise ValueError(f"invalid package index: {options.pypi_url}")
        self.url = url
        self.install = install
        self.pypi_url = pypi
        self._fetch: MarketplaceFetch = options.fetch or default_marketplace_fetch
        self._download: MarketplaceFetch = options.download or options.fetch or default_wheel_fetch
        self._pip: PipRunner = options.pip or default_pip_runner
        self._timeout = options.timeout
        self._server_version = options.server_version
        self.trusted_dependencies = [t for t in options.trusted_dependencies or [] if isinstance(t, str) and t.strip()]
        self._deps = deps
        self._locks: dict[int, asyncio.Lock] = {}

    @property
    def server_version(self) -> Optional[str]:
        """The running server's version (``qodalis-cli-server``)."""
        if self._server_version:
            return self._server_version
        try:
            return metadata.version(SERVER_CORE_PACKAGE)
        except metadata.PackageNotFoundError:
            return None

    @property
    def _plugins(self) -> Any:
        return self._deps.plugins

    def info(self) -> dict[str, Any]:
        """``GET /``: the marketplace, the policy, this platform and the server's version."""
        return {
            "marketplaceUrl": self.url, "install": self.install, "platform": SERVER_PLUGIN_PLATFORM,
            "serverVersion": self.server_version, "enabled": bool(self._plugins.enabled),
        }

    # ------------------------------------------------------------------ browsing

    def _platform_query(self) -> dict[str, str]:
        qs = {"kind": CLI_SERVER_PLUGIN_KIND, "platform": SERVER_PLUGIN_PLATFORM}
        if self.server_version:
            qs["serverVersion"] = self.server_version
        return qs

    async def search(self, query: Optional[str] = None, page: Optional[int] = None, page_size: Optional[int] = None) -> dict[str, Any]:
        """``GET /plugins``: the marketplace's server plugins with a Python package, annotated."""
        qs = self._platform_query()
        if query and query.strip():
            qs["query"] = query.strip()[:200]
        if page is not None:
            qs["page"] = str(page)
        if page_size is not None:
            qs["pageSize"] = str(page_size)
        body = await self._get_json(f"{self.url}{MARKETPLACE_API_PATH}/plugins?{urlencode(qs)}")
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise DashboardMarketplaceError(502, "marketplace unreachable")
        everything = body["items"]
        items = [i for i in everything if isinstance(i, dict) and is_valid_server_plugin_name(i.get("name")) and i.get("kind") == "server"]
        filtered = len(items) == len(everything)
        return {
            "items": [{**item, **self._annotate(item)} for item in items],
            "total": body["total"] if filtered and isinstance(body.get("total"), int) else len(items),
            "page": body["page"] if isinstance(body.get("page"), int) else (page or 1),
            "pageSize": body["pageSize"] if isinstance(body.get("pageSize"), int) else (page_size or len(items)),
        }

    async def details(self, name: Any) -> dict[str, Any]:
        """``GET /plugins/{name}``: one plugin with its platforms and versions (this platform's with ``allowed``)."""
        plugin = await self._fetch_plugin(name)
        own = _platforms_of(plugin).get(SERVER_PLUGIN_PLATFORM)
        out = dict(plugin)
        if isinstance(plugin.get("versions"), list):
            versions = []
            for v in plugin["versions"]:
                if own is None or not isinstance(v, dict) or v.get("platform", SERVER_PLUGIN_PLATFORM) != SERVER_PLUGIN_PLATFORM:
                    versions.append(v)
                    continue
                reason = self._version_blocked_reason(plugin, own, v)
                versions.append({**v, "allowed": reason is None, **({"blockedReason": reason} if reason else {})})
            out["versions"] = versions
        return {**out, **self._annotate(plugin)}

    def _annotate(self, item: Mapping[str, Any]) -> dict[str, Any]:
        platforms = _platforms_of(item)
        own = platforms.get(SERVER_PLUGIN_PLATFORM)
        record = self._record_of(item.get("name"))
        auto, auto_reason = _auto_install_of(item, own)
        platform: Optional[dict[str, Any]] = None
        if own is not None and _platform_versions(item, own) is None:
            # A list item: the marketplace says whether it runs here (it was given this server's version)
            compatible = item.get("compatible") is not False
            latest = item["latest"] if isinstance(item.get("latest"), str) else own.get("latest") if compatible and isinstance(own.get("latest"), str) else None
            platform = {"package": own["package"], "latest": latest, "installed": record["version"] if record else None, "compatible": compatible}
            if not compatible:
                platform["reason"] = item["reason"] if isinstance(item.get("reason"), str) else "not compatible with this server"
        elif own is not None:
            listed = _listed_versions(item, own)
            server = self.server_version
            fitting = next((v for v in listed if not server or satisfies_server_range(server, v.get("serverRange", own.get("serverRange")))), None)
            rng = listed[0].get("serverRange", own.get("serverRange")) if listed else own.get("serverRange")
            platform = {
                "package": own["package"], "latest": fitting["version"] if fitting else None,
                "installed": record["version"] if record else None, "compatible": fitting is not None,
            }
            if fitting is None:
                platform["reason"] = f"needs server {rng} (this server has {server})" if listed else f"no listed version for {SERVER_PLUGIN_PLATFORM}"
        blocked = self._blocked_reason(item, own, platform, auto)
        out: dict[str, Any] = {
            "platform": platform,
            "otherPlatforms": [p for p in platforms if p != SERVER_PLUGIN_PLATFORM],
            "autoInstall": auto,
        }
        if auto_reason:
            out["autoInstallReason"] = auto_reason
        listed_own = _listed_versions(item, own) if own is not None and _platform_versions(item, own) is not None else []
        needs = _needs_configuration_of(item, own) or (_needs_configuration_of({}, None, listed_own[0]) if listed_own else [])
        if needs:
            out["needsConfiguration"] = needs
        latest = platform.get("latest") if platform else None
        out["updateAvailable"] = bool(record and isinstance(latest, str) and _is_version(latest) and compare_server_plugin_versions(latest, record["version"]) > 0)
        out["allowed"] = blocked is None
        if blocked:
            out["blockedReason"] = blocked
        return out

    def _blocked_reason(self, item: Mapping[str, Any], own: Any, platform: Optional[Mapping[str, Any]], auto: bool) -> Optional[str]:
        if not self._plugins.enabled:
            return "server plugins disabled"
        if self.install == "off":
            return "install disabled"
        if self.install == "verified" and (item.get("publisher") or {}).get("verified") is not True:
            return "unverified publisher"
        if own is None:
            return f"not available for {SERVER_PLUGIN_PLATFORM}"
        if not auto:
            return "needs setup in code"
        if platform is not None and not platform.get("compatible"):
            return platform.get("reason")
        return None

    def _version_blocked_reason(self, plugin: Mapping[str, Any], own: Mapping[str, Any], v: Mapping[str, Any]) -> Optional[str]:
        auto, _ = _auto_install_of(plugin, own, v)
        server = self.server_version
        rng = v.get("serverRange", own.get("serverRange"))
        compatible = not server or satisfies_server_range(server, rng)
        platform = None if compatible else {"compatible": False, "reason": f"needs server {rng} (this server has {server})"}
        reason = self._blocked_reason(plugin, own, platform, auto)
        if reason:
            return reason
        if v.get("status", "listed") != "listed":
            return f"version {v.get('status')}"
        return None

    async def _get_json(self, url: str) -> Any:
        what = "marketplace unreachable" if url.startswith(self.url) else "package index unreachable"
        try:
            res = await self._fetch(url, {"Accept": "application/json"}, self._timeout)
        except Exception as exc:  # noqa: BLE001
            raise DashboardMarketplaceError(502, what) from exc
        if res.status == 404:
            return None
        if not res.ok:
            raise DashboardMarketplaceError(502, what)
        try:
            return res.json()
        except Exception as exc:  # noqa: BLE001
            raise DashboardMarketplaceError(502, what) from exc

    async def _fetch_plugin(self, name: Any) -> dict[str, Any]:
        if not is_valid_server_plugin_name(name):
            raise DashboardMarketplaceError(400, "invalid plugin name")
        plugin = await self._get_json(
            f"{self.url}{MARKETPLACE_API_PATH}/plugins/{quote(name, safe='')}?{urlencode(self._platform_query())}",
        )
        if not isinstance(plugin, dict):
            raise DashboardMarketplaceError(404, f"plugin not found: {name}")
        if plugin.get("name") != name:
            raise DashboardMarketplaceError(502, "marketplace unreachable")
        return plugin

    # ------------------------------------------------------------------ installed

    def _record_of(self, name: Any) -> Optional[dict[str, Any]]:
        return next((r for r in self._plugins.records() if r["name"] == name), None)

    def _loaded_of(self, name: str) -> Any:
        return next((s for s in self._plugins.states() if s.name == name and s.source == "marketplace" and s.status == "loaded"), None)

    def view(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """One installed plugin with how it runs in this process: ``loaded``, ``pending`` (runs after a restart),
        ``failed``, ``skipped``, ``needs-configuration`` (``missing``) or ``removed``."""
        state = next((s for s in self._plugins.states() if s.name == record["name"] and s.source == "marketplace"), None)
        loaded = state.version if state is not None and state.status == "loaded" else None
        error = record.get("error")
        missing = None
        if loaded == record["version"]:
            status = "loaded"
        elif state is not None and state.version == record["version"]:
            status = state.status
            error = state.error or error
            missing = state.missing
        else:
            status = "pending"
        out: dict[str, Any] = {
            "name": record["name"],
            "package": record["package"],
            "version": record["version"],
            "title": record.get("title") or record["name"],
            "icon": record.get("icon"),
            "description": record.get("description"),
            "installedAt": record.get("installedAt") or "",
            "installedBy": record.get("installedBy") or "",
            "status": status,
            "loadedVersion": loaded,
        }
        if error and status not in ("loaded", "pending"):
            out["error"] = error
        if missing:
            out["missing"] = list(missing)
        return out

    async def installed(self, check_updates: bool = False) -> list[dict[str, Any]]:
        """``GET /installed``: the installed plugins, then the removed ones still running; ``updateAvailable`` when
        the marketplace answers."""
        records = self._plugins.records()
        views = [self.view(r) for r in records]
        for s in self._plugins.states():
            if s.source != "marketplace" or s.status != "loaded" or any(r["name"] == s.name for r in records):
                continue
            views.append({
                "name": s.name, "package": s.package, "version": s.version, "title": s.name, "icon": None,
                "description": None, "installedAt": "", "installedBy": "", "status": "removed", "loadedVersion": s.version,
            })
        if not check_updates:
            return views

        async def check(v: dict[str, Any]) -> dict[str, Any]:
            if v["status"] == "removed":
                return v
            try:
                latest = (self._annotate(await self._fetch_plugin(v["name"])).get("platform") or {}).get("latest")
            except Exception:  # noqa: BLE001
                return v
            return {**v, "updateAvailable": isinstance(latest, str) and _is_version(latest) and compare_server_plugin_versions(latest, v["version"]) > 0}

        return list(await asyncio.gather(*(check(v) for v in views)))

    # ------------------------------------------------------------------ changes

    def _lock(self) -> asyncio.Lock:
        loop = id(asyncio.get_running_loop())
        lock = self._locks.get(loop)
        if lock is None:
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    async def install_plugin(self, name: Any, version: Any, actor: DashboardMarketplaceActor) -> dict[str, Any]:
        """``POST /installed``: installs the Python package of ``name`` (the newest compatible version, or ``version``)."""
        async with self._lock():
            plugin, own, target = await self._prepare(name, version)
            existing = self._record_of(plugin["name"])
            if existing is not None:
                raise DashboardMarketplaceError(409, f"already installed: {plugin['name']}@{existing['version']}")
            bundled = self._bundled(own["package"])
            record = (
                self._bundled_record(plugin, own, bundled, actor) if bundled is not None
                else await self._install_package(plugin, own, target, actor, None)
            )
            self._plugins.save([*(r for r in self._plugins.records() if r["name"] != record["name"]), record])
            restart_required = self._sync_restart(record["name"], f"Install {record['name']} {record['version']}")
            await self._changed("install", record, actor)
            return {**self.view(record), "restartRequired": restart_required, **self._needs(plugin, own, target)}

    async def update_plugin(self, name: Any, version: Any, actor: DashboardMarketplaceActor) -> dict[str, Any]:
        """``PUT /installed/{name}``: updates to ``version`` (default: the newest compatible); ``changed: False`` when
        it already is."""
        async with self._lock():
            if not is_valid_server_plugin_name(name):
                raise DashboardMarketplaceError(400, "invalid plugin name")
            current = self._record_of(name)
            if current is None:
                raise DashboardMarketplaceError(404, f"not installed: {name}")
            if current.get("bundled") is True:
                raise DashboardMarketplaceError(
                    409, f"{current['package']} comes with the server: it is updated with the server, not from here",
                )
            plugin, own, target = await self._prepare(name, version)
            if canonicalize_name(own["package"]) != canonicalize_name(current["package"]):
                raise DashboardMarketplaceError(
                    409, f"the marketplace's package is {own['package']}, {current['package']} is installed: remove it first",
                )
            if target["version"] == current["version"]:
                return {
                    **self.view(current), "restartRequired": self._restart_pending(current["name"]), "changed": False,
                    "previousVersion": current["version"],
                }
            record = await self._install_package(plugin, own, target, actor, current)
            self._plugins.save([record if r["name"] == record["name"] else r for r in self._plugins.records()])
            restart_required = self._sync_restart(
                record["name"], f"Update {record['name']} {current['version']} -> {record['version']}",
            )
            await self._changed("update", record, actor, current["version"])
            return {
                **self.view(record), "restartRequired": restart_required, "changed": True,
                "previousVersion": current["version"], **self._needs(plugin, own, target),
            }

    async def remove_plugin(self, name: Any, actor: DashboardMarketplaceActor) -> dict[str, Any]:
        """``DELETE /installed/{name}``: removes the package (its dependencies stay); it runs until a restart."""
        async with self._lock():
            if not is_valid_server_plugin_name(name):
                raise DashboardMarketplaceError(400, "invalid plugin name")
            if not self._plugins.enabled:
                raise DashboardMarketplaceError(403, "server plugins disabled")
            current = self._record_of(name)
            if current is None:
                raise DashboardMarketplaceError(404, f"not installed: {name}")
            if current.get("bundled") is not True:
                await asyncio.to_thread(uninstall_from, self._plugins.dir, current["package"])
                self._remove_wheel(current)
            self._plugins.save([r for r in self._plugins.records() if r["name"] != current["name"]])
            restart_required = self._sync_restart(current["name"], f"Remove {current['name']} {current['version']}")
            await self._changed("remove", current, actor)
            view = self.view(current)
            if self._loaded_of(current["name"]) is not None:
                view["status"] = "removed"
            return {**view, "restartRequired": restart_required}

    @staticmethod
    def _needs(plugin: Mapping[str, Any], own: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
        needs = _needs_configuration_of(plugin, own, target)
        return {"needsConfiguration": needs} if needs else {}

    async def _prepare(self, name: Any, version: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """The plugin, its Python package and the version to install, after the policy, platform and range checks."""
        if not is_valid_server_plugin_name(name):
            raise DashboardMarketplaceError(400, "invalid plugin name")
        if version is not None and not _is_version(version):
            raise DashboardMarketplaceError(400, "invalid version")
        if not self._plugins.enabled:
            raise DashboardMarketplaceError(403, "server plugins disabled")
        plugin = await self._fetch_plugin(name)
        if plugin.get("kind") != "server":
            raise DashboardMarketplaceError(400, f"not a server plugin: {name}")
        if self.install == "off":
            raise DashboardMarketplaceError(403, "install disabled")
        if self.install == "verified" and (plugin.get("publisher") or {}).get("verified") is not True:
            raise DashboardMarketplaceError(403, "publisher not verified")
        own = _platforms_of(plugin).get(SERVER_PLUGIN_PLATFORM)
        if own is None:
            raise DashboardMarketplaceError(400, f"not available for {SERVER_PLUGIN_PLATFORM}: {name}")
        if not _is_package(own.get("package")):
            raise DashboardMarketplaceError(502, "invalid marketplace record: package")
        listed = _listed_versions(plugin, own)
        server = self.server_version
        if version:
            target = next((v for v in listed if v["version"] == version), None)
            if target is None:
                raise DashboardMarketplaceError(404, f"version not found: {name}@{version}")
        else:
            target = next((v for v in listed if not server or satisfies_server_range(server, v.get("serverRange", own.get("serverRange")))), None)
            target = target or (listed[0] if listed else None)
            if target is None:
                raise DashboardMarketplaceError(404, f"no installable version: {name}")
        auto, reason = _auto_install_of(plugin, own, target)
        if not auto:
            raise DashboardMarketplaceError(400, f"needs setup in code{': ' + reason if reason else ''}")
        rng = target.get("serverRange", own.get("serverRange"))
        if server and not satisfies_server_range(server, rng):
            raise DashboardMarketplaceError(
                400, f"incompatible: {own['package']}=={target['version']} needs server {rng} (this server has {server})",
            )
        wanted = canonicalize_name(own["package"])
        for p in self._deps.installed_plugins():
            if canonicalize_name(str(getattr(p, "id", ""))) == wanted and getattr(p, "source", None) != "marketplace":
                raise DashboardMarketplaceError(409, f"already installed by the host: {own['package']}")
        return plugin, own, target

    def _bundled(self, package: str) -> Any:
        """The package as the server's environment has it (it comes with the server, e.g. the filesystem plugin), or
        ``None``."""
        paths = [p for p in sys.path if os.path.abspath(p or ".") != self._plugins.dir]
        return next(iter(metadata.distributions(name=package, path=paths)), None)

    def _bundled_record(
        self, plugin: Mapping[str, Any], own: Mapping[str, Any], dist: Any, actor: DashboardMarketplaceActor,
    ) -> dict[str, Any]:
        """A package already in the server's environment is enabled, not downloaded: its entry point is checked and
        it is recorded with ``bundled`` (its version the environment's)."""
        if not list(dist.entry_points.select(group=CLI_SERVER_PLUGIN_ENTRY_GROUP)):
            raise DashboardMarketplaceError(
                400, f"not a server plugin package: {own['package']} (no {CLI_SERVER_PLUGIN_ENTRY_GROUP} entry point)",
            )
        title = next((t for t in (plugin.get("title"), plugin.get("name")) if isinstance(t, str) and t.strip()), plugin["name"])
        record: dict[str, Any] = {
            "name": plugin["name"], "package": own["package"], "version": dist.version, "integrity": "",
            "bundled": True, "title": title.strip()[:60],
        }
        if isinstance(plugin.get("icon"), str):
            record["icon"] = plugin["icon"]
        description = plugin.get("description") if isinstance(plugin.get("description"), str) else dist.metadata.get("Summary")
        if isinstance(description, str) and description:
            record["description"] = description[:500]
        record["installedAt"] = _now_iso()
        record["installedBy"] = actor.user_name
        return record

    async def _install_package(
        self, plugin: Mapping[str, Any], own: Mapping[str, Any], target: Mapping[str, Any],
        actor: DashboardMarketplaceActor, current: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Downloads the wheel, checks it and installs it with pip (staged, then moved in).  Throws without changing
        anything when a check fails."""
        package, version = own["package"], target["version"]
        release = await self._get_json(f"{self.pypi_url}/pypi/{quote(package, safe='')}/{quote(version, safe='')}/json")
        if not isinstance(release, dict):
            raise DashboardMarketplaceError(404, f"not on PyPI: {package}=={version}")
        wheel = _choose_wheel(release.get("urls") if isinstance(release.get("urls"), list) else [])
        if wheel is None:
            raise DashboardMarketplaceError(400, f"no wheel of {package}=={version} for this Python ({sys.version_info.major}.{sys.version_info.minor})")
        integrity = _hex_to_integrity((wheel.get("digests") or {}).get("sha256"))
        url = wheel.get("url")
        if integrity is None:
            raise DashboardMarketplaceError(502, "the PyPI record has no sha256")
        if not isinstance(url, str) or not re.match(r"^https?://", url, re.IGNORECASE):
            raise DashboardMarketplaceError(502, "the PyPI record has no wheel url")
        # The marketplace's hash of the same file (its files list), else of the file it chose
        files = target.get("files") if isinstance(target.get("files"), list) else []
        listed = next((f.get("integrity") for f in files if isinstance(f, dict) and f.get("path") == wheel.get("filename")), None)
        expected = listed if isinstance(listed, str) else target.get("integrity") if not files else None
        if isinstance(expected, str) and _SHA256.fullmatch(expected) and expected != integrity:
            raise DashboardMarketplaceError(502, "integrity mismatch: the marketplace and PyPI disagree")
        data = await self._download_wheel(url)
        if sha256_integrity(data) != integrity:
            raise DashboardMarketplaceError(502, f"integrity mismatch: {package}=={version}")

        # The wheel itself must be this package, a server plugin, for this server
        info = read_wheel(data)
        if canonicalize_name(info.name) != canonicalize_name(package) or info.version != version:
            raise DashboardMarketplaceError(502, f"the wheel is {info.name}=={info.version}, not {package}=={version}")
        if not info.entry_points:
            raise DashboardMarketplaceError(
                400, f"not a server plugin package: {package} (no {CLI_SERVER_PLUGIN_ENTRY_GROUP} entry point)",
            )
        server = self.server_version
        for text in info.requires:
            try:
                req = Requirement(text)
            except InvalidRequirement:
                continue
            if canonicalize_name(req.name) in SERVER_CORE_PACKAGES and server and req.specifier and not req.specifier.contains(server, prereleases=True):
                raise DashboardMarketplaceError(
                    400, f"incompatible: {package}=={version} needs {req.name}{req.specifier} (this server has {server})",
                )

        # Its dependencies: pinned by the marketplace (hash-checked), else only those the host trusts; never a
        # package of the server's environment.  Decided before anything is stored.
        directory = self._plugins.dir
        plan = self._dependency_plan(info.requires, target, directory)

        # Verified: pip installs the wheel (kept) and the missing dependencies into a staging directory
        filename = os.path.basename(str(wheel["filename"]))
        wheels = os.path.join(directory, "wheels")
        os.makedirs(wheels, exist_ok=True)
        wheel_path = os.path.join(wheels, filename)
        with open(wheel_path, "wb") as f:
            f.write(data)
        staging = os.path.join(directory, f".staging-{secrets.token_hex(4)}")
        try:
            await self._run_pip(["install", "--target", staging, "--no-deps", *self._pip_flags(), wheel_path], "pip install")
            await self._install_dependencies(plan, staging)
            await asyncio.to_thread(_drop_host_packages, staging, directory, package)
            if current is not None:
                await asyncio.to_thread(uninstall_from, directory, current["package"])
                self._remove_wheel(current)
            await asyncio.to_thread(_move_into, staging, directory, package)
        except BaseException:
            if current is None or current.get("file") != filename:
                try:
                    os.remove(wheel_path)
                except OSError:
                    pass
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        title = next((t for t in (plugin.get("title"), plugin.get("name")) if isinstance(t, str) and t.strip()), plugin["name"])
        record: dict[str, Any] = {
            "name": plugin["name"],
            "package": package,
            "version": version,
            "integrity": integrity,
            "file": filename,
            "title": title.strip()[:60],
        }
        if isinstance(plugin.get("icon"), str):
            record["icon"] = plugin["icon"]
        description = plugin.get("description") if isinstance(plugin.get("description"), str) else info.summary
        if isinstance(description, str) and description:
            record["description"] = description[:500]
        record["installedAt"] = _now_iso()
        record["installedBy"] = actor.user_name
        return record

    def _dependency_plan(self, requires: list[str], target: Mapping[str, Any], directory: str) -> dict[str, Any]:
        """What pip installs besides the wheel: ``{"pinned": [...]}`` (exact versions with their sha256),
        ``{"trusted": [...]}`` (requirements the host allows) or nothing."""
        missing = missing_requirements(requires, directory)
        pins = pinned_dependencies(target)
        if pins is not None:
            # Every pin the server lacks (they are the whole tree: installed with --no-deps)
            wanted = [
                p for p in pins
                if canonicalize_name(p["id"]) not in SERVER_CORE_PACKAGES and _host_version(p["id"], directory) is None
                and _dir_version(p["id"], directory) != p["version"]
            ]
            pinned = {canonicalize_name(p["id"]) for p in pins}
            for text in missing:
                if canonicalize_name(Requirement(text).name) not in pinned:
                    raise DashboardMarketplaceError(400, f"untrusted dependency: {Requirement(text).name} (not pinned by the marketplace)")
            return {"pinned": wanted}
        untrusted = [Requirement(t).name for t in missing if not is_trusted_dependency(Requirement(t).name, self.trusted_dependencies)]
        if untrusted:
            raise DashboardMarketplaceError(400, f"untrusted dependency: {', '.join(untrusted)} (the marketplace pins none and the server does not trust it)")
        return {"trusted": missing}

    async def _install_dependencies(self, plan: dict[str, Any], staging: str) -> None:
        if plan.get("pinned"):
            os.makedirs(staging, exist_ok=True)
            requirements = os.path.join(staging, "..", f".requirements-{secrets.token_hex(4)}.txt")
            with open(requirements, "w", encoding="utf-8") as f:
                for p in plan["pinned"]:
                    f.write(f"{p['id']}=={p['version']} --hash=sha256:{p['sha256']}\n")
            try:
                await self._run_pip(
                    ["install", "--target", staging, "--require-hashes", "--no-deps", *self._pip_flags(), "-r", requirements],
                    "pip install of its dependencies",
                )
            finally:
                try:
                    os.remove(requirements)
                except OSError:
                    pass
        elif plan.get("trusted"):
            await self._run_pip(["install", "--target", staging, *self._pip_flags(), *plan["trusted"]], "pip install of its dependencies")

    def _pip_flags(self) -> list[str]:
        return ["--disable-pip-version-check", "--no-input", "--no-compile", "--only-binary", ":all:", "--quiet"]

    async def _run_pip(self, args: list[str], what: str) -> None:
        try:
            result = await self._pip(args)
        except Exception as exc:  # noqa: BLE001
            raise DashboardMarketplaceError(502, f"{what} failed: {exc}") from exc
        if result.code != 0:
            lines = [ln.strip() for ln in (result.stderr or "").splitlines() if ln.strip()]
            line = next((ln for ln in lines if re.search(r"error", ln, re.IGNORECASE)), lines[-1] if lines else "")
            raise DashboardMarketplaceError(502, f"{what} failed (exit {result.code}){': ' + line[:300] if line else ''}")

    def _remove_wheel(self, record: Mapping[str, Any]) -> None:
        file = record.get("file")
        if isinstance(file, str) and file == os.path.basename(file) and file.endswith(".whl"):
            try:
                os.remove(os.path.join(self._plugins.dir, "wheels", file))
            except OSError:
                pass

    async def _download_wheel(self, url: str) -> bytes:
        try:
            res = await self._download(url, {}, max(self._timeout, 60.0))
        except Exception as exc:  # noqa: BLE001
            raise DashboardMarketplaceError(502, "download failed: wheel") from exc
        if not res.ok:
            raise DashboardMarketplaceError(502, f"download failed: wheel ({res.status})")
        if len(res.body) > SERVER_PLUGIN_MAX_WHEEL_BYTES:
            raise DashboardMarketplaceError(502, "wheel too large")
        return res.body

    def _restart_pending(self, name: str) -> bool:
        """True when the installed list differs from what this process runs for ``name``."""
        record = self._record_of(name)
        loaded = self._loaded_of(name)
        return (loaded is None or loaded.version != record["version"]) if record else loaded is not None

    def _sync_restart(self, name: str, description: str) -> bool:
        """Marks (or clears, when back to what runs) the restart for a plugin; true when one is needed."""
        pending = self._restart_pending(name)
        restart = self._deps.restart
        if restart is not None:
            restart_id = f"plugin:{name}"
            restart.clear_required(restart_id)
            if pending:
                restart.mark_required(restart_id, "plugins", description)
        return pending

    async def _changed(
        self, action: str, record: Mapping[str, Any], actor: DashboardMarketplaceActor, previous: Optional[str] = None,
    ) -> None:
        properties: dict[str, Any] = {"name": record["name"], "package": record["package"], "version": record["version"]}
        if previous:
            properties["previousVersion"] = previous
        sink = self._deps.audit()
        if sink is not None:
            try:
                result = sink(AuditEntry(
                    timestamp=int(time.time() * 1000),
                    event=f"server.plugin.{action}",
                    username=actor.user_name,
                    user_id=actor.user_id,
                    actor=actor.user_name,
                    actor_id=actor.user_id,
                    target=record["name"],
                    details=f"{record['package']} {previous} -> {record['version']}" if previous else f"{record['package']}=={record['version']}",
                    properties=properties,
                ))
                if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                    await result
            except Exception as exc:  # noqa: BLE001 - the change is made; the audit failing is logged
                logger.error("audit of a server plugin change failed: %s", exc)
        if self._deps.publish is not None:
            try:
                await self._deps.publish(SERVER_PLUGINS_CHANGED_EVENT, {"action": action, **properties})
            except Exception as exc:  # noqa: BLE001
                logger.error("publishing %s failed: %s", SERVER_PLUGINS_CHANGED_EVENT, exc)


def _drop_host_packages(staging: str, directory: str, package: str) -> None:
    """Removes from the staging directory every distribution the server's environment has (pip may bring them as
    dependencies of a dependency): a plugin never shadows the server's packages."""
    if not os.path.isdir(staging):
        return
    for entry in os.listdir(staging):
        if not entry.endswith(".dist-info"):
            continue
        name = entry[: -len(".dist-info")].rsplit("-", 1)[0]
        if canonicalize_name(name) == canonicalize_name(package):
            continue
        if canonicalize_name(name) in SERVER_CORE_PACKAGES or _host_version(name, directory) is not None:
            uninstall_from(staging, name)


def _move_into(staging: str, directory: str, package: str) -> None:
    """Moves what pip installed in ``staging`` into the plugins directory: the plugin's files replace any left over;
    a dependency already there is kept (another plugin may use it)."""
    if not os.path.isdir(staging):
        raise DashboardMarketplaceError(502, "pip installed nothing")
    own_infos = {os.path.basename(p) for p in _dist_info_dirs(staging, package)}
    if not own_infos:
        raise DashboardMarketplaceError(502, f"pip did not install {package}")
    own_top: set[str] = set()
    for info in own_infos:
        record = os.path.join(staging, info, "RECORD")
        if os.path.isfile(record):
            with open(record, encoding="utf-8") as f:
                for line in f:
                    rel = line.split(",", 1)[0].strip()
                    if rel and not rel.startswith(".."):
                        own_top.add(rel.split("/", 1)[0])
    own_top |= own_infos
    for entry in os.listdir(staging):
        source = os.path.join(staging, entry)
        target = os.path.join(directory, entry)
        if entry in ("bin", "__pycache__"):
            continue
        if os.path.lexists(target):
            if entry not in own_top:
                continue  # a dependency another plugin brought
            if os.path.isdir(target) and not os.path.islink(target):
                if entry.endswith(".dist-info"):
                    shutil.rmtree(target)
                else:
                    _merge(source, target)
                    continue
            else:
                os.remove(target)
        shutil.move(source, target)


def _merge(source: str, target: str) -> None:
    """Copies a staged package directory over an existing one (a namespace package two plugins share)."""
    for root, _dirs, files in os.walk(source):
        rel = os.path.relpath(root, source)
        dest = os.path.join(target, rel) if rel != "." else target
        os.makedirs(dest, exist_ok=True)
        for name in files:
            shutil.copy2(os.path.join(root, name), os.path.join(dest, name))


__all__ = [
    "DEFAULT_PYPI_URL",
    "PLUGINS_INSTALL_PERMISSION",
    "PipRunResult",
    "PipRunner",
    "SERVER_CORE_PACKAGE",
    "SERVER_CORE_PACKAGES",
    "SERVER_PLUGINS_CHANGED_EVENT",
    "SERVER_PLUGIN_MAX_WHEEL_BYTES",
    "SERVER_PLUGIN_PLATFORM",
    "ServerPluginMarketplace",
    "ServerPluginMarketplaceDeps",
    "ServerPluginMarketplaceOptions",
    "compare_server_plugin_versions",
    "default_pip_runner",
    "is_trusted_dependency",
    "missing_requirements",
    "pinned_dependencies",
    "read_wheel",
    "satisfies_server_range",
    "sha256_integrity",
    "uninstall_from",
]
