"""The server's plugins loaded through their entry point (``create_plugin(context)``): those the host added in code
(``builder.add_plugin(entry, options)``) and those installed at runtime (the admin plugin's server marketplace:
``<data dir>/server-plugins`` holds the packages, ``pip install --target``, and ``installed.json``).  At startup the
directory is added to ``sys.path`` after the server's own packages; every plugin loads in its declared ``order``
(``CLI_PLUGIN_LOAD_ORDER``) with a :class:`CliPluginContext`.  A plugin whose required settings have no value is
``needs-configuration``; one that fails is logged and its error kept in ``installed.json``: startup goes on.  The
same ``installed.json`` and states as the Node server."""

from __future__ import annotations

import importlib.metadata as metadata
import inspect
import json
import logging
import os
import re
import secrets
import sys
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from qodalis_cli_server_abstractions.config import (
    CliConfigSectionDefinition,
    CliConfigSettingDefinition,
    CliStoredConfigProvider,
    JsonFileConfigStore,
)
from qodalis_cli_server_abstractions.plugin_entry import (
    CLI_SERVER_PLUGIN_ENTRY_GROUP,
    CLI_SERVER_PLUGIN_FAILED,
    CLI_SERVER_PLUGIN_LOADED,
    CLI_SERVER_PLUGIN_NEEDS_CONFIGURATION,
    CLI_SERVER_PLUGIN_SKIPPED,
    CLI_SERVER_PLUGINS_DIR,
    CLI_SERVER_PLUGINS_FILE,
    CliPluginContext,
    CLI_PLUGIN_LOAD_ORDER,
    CliPluginEntryInfo,
    CliPluginNeedsConfiguration,
    CliServerPluginState,
)

from ..models.cli_installed_plugin import CliInstalledPlugin, short_plugin_name

logger = logging.getLogger("qodalis_cli.server_plugins")

#: The server's data directory unless ``data_dir`` is given: ``./qcli-data`` (from the working directory).
DEFAULT_DATA_DIR = "qcli-data"
#: The settings file of the plugins while the server keeps its settings in memory (no default store set).
CLI_PLUGIN_SETTINGS_FILE = "plugin-settings.json"

_PLUGIN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\Z")
_DIST_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?\Z")
_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+!_-]{0,63}\Z")


def is_valid_server_plugin_name(name: Any) -> bool:
    """True for a marketplace plugin id (``jobs``, ``data-explorer-postgres``)."""
    return isinstance(name, str) and _PLUGIN_ID.match(name) is not None


def is_valid_server_plugin_package(name: Any) -> bool:
    """True for a PyPI distribution name (``qodalis-cli-server-plugin-weather``)."""
    return isinstance(name, str) and _DIST_NAME.match(name) is not None


def is_valid_server_plugin_version(version: Any) -> bool:
    """True for a PEP 440 looking version (``1.0.0b31``, ``2.1.0``)."""
    return isinstance(version, str) and _VERSION.match(version) is not None


def normalize_distribution(name: str) -> str:
    """PEP 503 normalized name (``Qodalis_CLI.x`` -> ``qodalis-cli-x``)."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class CliServerPluginsOptions:
    """The ``server_plugins`` option of ``create_cli_server``."""

    #: ``<data dir>/server-plugins`` by default.
    dir: Optional[str] = None


@dataclass
class CliServerPluginsHost:
    """What the loader needs from the server (set by ``create_cli_server``)."""

    builder: Any
    config: Any
    event_bus: Any
    restart: Any
    #: Mounts a plugin result (``mount_plugin``), its routes ahead of a host's catch-all.
    mount: Callable[[Any], None]
    #: Wires what the plugins set on the builder (filesystem storage, data explorer sources).
    after_load: Callable[[], None]


class _Skipped(Exception):
    """A plugin left out on purpose (the host has the same package)."""


def _is_mountable(value: Any) -> bool:
    return isinstance(getattr(value, "prefix", None), str) and getattr(value, "router", None) is not None


def _is_module(value: Any) -> bool:
    return isinstance(getattr(value, "name", None), str) and isinstance(getattr(value, "processors", None), (list, tuple))


def _first_line(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text.split("\n")[0][:500]


class CliServerPlugins:
    """The server's runtime-installed plugins (``builder.server_plugins``, ``result.server_plugins``)."""

    def __init__(self, data_dir: str = DEFAULT_DATA_DIR, options: Any = None) -> None:
        self.data_dir = os.path.abspath(data_dir)
        self.enabled = options is not False
        custom = getattr(options, "dir", None) if options not in (None, False) else None
        if isinstance(options, dict):
            custom = options.get("dir")
        self.dir = os.path.abspath(custom or os.path.join(self.data_dir, CLI_SERVER_PLUGINS_DIR))
        self._states: list[CliServerPluginState] = []
        self._host: Optional[CliServerPluginsHost] = None
        self._loaded = False
        self._code: list[_Candidate] = []
        self._services = CliPluginServices()
        self._settings_file: Any = None
        self._config_store_pending = False
        self._parked: list[str] = []

    def attach(self, host: CliServerPluginsHost) -> None:
        """Set by the server; :meth:`ready` (the app's startup) loads the plugins."""
        self._host = host

    # ------------------------------------------------------------------ the installed list

    def records(self) -> list[dict[str, Any]]:
        """The installed list (``installed.json``); empty when there is none or it is not readable."""
        path = os.path.join(self.dir, CLI_SERVER_PLUGINS_FILE)
        if not os.path.isfile(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                plugins = json.load(f).get("plugins")
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s is not readable JSON: %s", path, exc)
            return []
        if not isinstance(plugins, list):
            return []
        return [
            r for r in plugins
            if isinstance(r, dict) and is_valid_server_plugin_name(r.get("name"))
            and is_valid_server_plugin_package(r.get("package")) and is_valid_server_plugin_version(r.get("version"))
            and isinstance(r.get("integrity"), str)
        ]

    def save(self, records: list[dict[str, Any]]) -> None:
        """Writes the installed list atomically."""
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, CLI_SERVER_PLUGINS_FILE)
        temp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(json.dumps({"plugins": records}, indent=2, ensure_ascii=False) + "\n")
            os.replace(temp, path)
        except BaseException:
            try:
                os.remove(temp)
            except OSError:
                pass
            raise

    def states(self) -> list[CliServerPluginState]:
        """What this process loaded at startup (empty before :meth:`ready`)."""
        return [CliServerPluginState(**vars(s)) for s in self._states]

    # ------------------------------------------------------------------ loading

    @property
    def services(self) -> "CliPluginServices":
        """What plugins share (``context.services``, ``builder.plugin_services``)."""
        return self._services

    def add(self, entry: Any, options: Optional[dict[str, Any]] = None) -> None:
        """A plugin the host adds in code (``builder.add_plugin``): its entry module (or ``create_plugin`` itself),
        whose module names the plugin (``plugin = CliPluginEntryInfo(id, package)``); loaded with the installed ones,
        in order, with ``options`` as its settings' defaults (origin ``code``)."""
        create_plugin, module = _entry_of(entry)
        if create_plugin is None:
            raise TypeError("add_plugin: the entry has no create_plugin(context)")
        info = _info_of(getattr(module, "plugin", None)) or _info_of(getattr(create_plugin, "plugin", None))
        if info is None:
            raise ValueError("add_plugin: the entry's module names no plugin = CliPluginEntryInfo(id, package)")
        if self._loaded:
            raise RuntimeError(f"add_plugin({info.id}): the plugins are already loaded; add it before the server starts")
        if any(normalize_distribution(c.package) == normalize_distribution(info.package) for c in self._code):
            raise ValueError(f"add_plugin: {info.package} is added twice")
        version = info.version
        if version is None:
            try:
                version = metadata.version(info.package)
            except metadata.PackageNotFoundError:
                version = "0.0.0"
        self._code.append(_Candidate(
            name=info.id, package=info.package, version=version, source="code", entry=create_plugin,
            order=_order_of(getattr(create_plugin, "order", None), getattr(module, "order", None)),
            options=dict(options or {}), title=info.title, icon=info.icon, description=info.description,
        ))

    async def ready(self) -> list[CliServerPluginState]:
        """Loads the plugins added in code and the installed ones once (the app's startup calls it; tests and hosts
        may call it before)."""
        if self._host is None or self._loaded:
            return self.states()
        self._loaded = True
        try:
            await self._load_all(self._host)
        except Exception as exc:  # noqa: BLE001
            logger.error("loading the server plugins failed: %s", exc)
        return self.states()

    def _add_to_path(self) -> None:
        # After the server's own packages: a plugin never replaces them
        if self.dir not in sys.path:
            sys.path.append(self.dir)
        importlib_invalidate()

    def _host_distribution(self, package: str) -> Any:
        """The package as the server's own environment has it (outside the plugins directory), or ``None``."""
        paths = [p for p in sys.path if os.path.abspath(p or ".") != self.dir]
        return next(iter(metadata.distributions(name=package, path=paths)), None)

    def _prepare(self, host: CliServerPluginsHost, record: dict[str, Any]) -> "_Candidate":
        """An installed package checked and its entry loaded (raises _Skipped or an error)."""
        package = record["package"]
        normalized = normalize_distribution(package)
        installed = getattr(host.builder, "installed_plugins", None) or []
        if any(normalize_distribution(c.package) == normalized for c in self._code) or any(
            normalize_distribution(p.id) == normalized and getattr(p, "source", None) not in ("marketplace", "code")
            for p in installed
        ):
            raise _Skipped("already installed by the host")
        own = self._host_distribution(package)
        bundled = record.get("bundled") is True
        if own is not None and not bundled:
            # Its modules would be imported from the server's environment, not from the plugins directory
            raise _Skipped(f"installed in the server's environment ({package} {own.version})")
        if bundled:
            # Enabled from the marketplace, its package in the server's environment (the filesystem plugin)
            if own is None:
                raise ValueError(f"not installed in the server's environment: {package}")
            dist = own
        else:
            dist = next(iter(metadata.distributions(name=package, path=[self.dir])), None)
            if dist is None:
                raise ValueError(f"not installed in {self.dir}")
            if dist.version != record["version"]:
                raise ValueError(f"the installed version is {dist.version}, not {record['version']}")
        entries = list(dist.entry_points.select(group=CLI_SERVER_PLUGIN_ENTRY_GROUP))
        if not entries:
            raise ValueError(f"not a server plugin (no {CLI_SERVER_PLUGIN_ENTRY_GROUP} entry point)")
        entry = entries[0]
        create_plugin = entry.load()
        module = sys.modules.get(entry.module)
        origin = os.path.realpath(getattr(module, "__file__", "") or "")
        if not bundled and not origin.startswith(os.path.realpath(self.dir) + os.sep):
            raise ValueError(f"the entry {entry.value} is outside the plugins directory")
        if not callable(create_plugin):
            raise ValueError(f"the entry {entry.value} is not a function create_plugin(context)")
        info = _info_of(getattr(module, "plugin", None))
        summary = dist.metadata.get("Summary") if dist.metadata else None
        return _Candidate(
            name=record["name"], package=package, version=dist.version if bundled else record["version"], source="marketplace",
            entry=create_plugin, order=_order_of(getattr(create_plugin, "order", None), getattr(module, "order", None)),
            options={}, title=record.get("title") or (info.title if info else None) or short_plugin_name(package),
            icon=record.get("icon"), description=record.get("description") or summary or None, record=record,
        )

    async def _load_all(self, host: CliServerPluginsHost) -> None:
        candidates: list[_Candidate] = list(self._code)
        records = self.records() if self.enabled else []
        if records:
            self._add_to_path()
        for record in records:
            try:
                if any(normalize_distribution(c.package) == normalize_distribution(record["package"]) and c.source == "marketplace" for c in candidates):
                    raise _Skipped("listed twice")
                candidates.append(self._prepare(host, record))
            except BaseException as exc:  # noqa: BLE001 - a plugin never stops the server (SyntaxError, SystemExit, ...)
                if isinstance(exc, KeyboardInterrupt):
                    raise
                self._fail(record["name"], record["package"], record["version"], "marketplace", CLI_PLUGIN_LOAD_ORDER.DEFAULT, exc)
        # Stable: code plugins first on a tie
        ordered = sorted(candidates, key=lambda c: c.order)
        self._config_store_pending = any(c.order == CLI_PLUGIN_LOAD_ORDER.CONFIG_STORE for c in ordered)
        for candidate in ordered:
            if candidate.order > CLI_PLUGIN_LOAD_ORDER.CONFIG_STORE:
                self._settle_config_store(host)
            try:
                await self._load_one(host, candidate)
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, KeyboardInterrupt):
                    raise
                self._fail(candidate.name, candidate.package, candidate.version, candidate.source, candidate.order, exc)
        self._settle_config_store(host)
        if self._parked_moved_need_ready(host):
            await host.config.ready()
        if candidates:
            try:
                host.after_load()
            except Exception as exc:  # noqa: BLE001
                logger.error("wiring the server plugins failed: %s", exc)
        self._save_states(records)

    @staticmethod
    def _parked_moved_need_ready(host: CliServerPluginsHost) -> bool:
        registry = getattr(host.builder, "config_registry", None)
        return bool(getattr(registry, "_pending", None))

    def _fail(self, name: str, package: str, version: str, source: str, order: int, exc: BaseException) -> None:
        skipped = isinstance(exc, _Skipped)
        state = CliServerPluginState(
            name, package, version, CLI_SERVER_PLUGIN_SKIPPED if skipped else CLI_SERVER_PLUGIN_FAILED,
            error=_first_line(exc), source=source, order=order,
        )
        self._states.append(state)
        (logger.warning if skipped else logger.error)("%s (%s %s) not loaded: %s", name, package, version, state.error)

    def _save_states(self, records: list[dict[str, Any]]) -> None:
        """The status of the installed plugins in installed.json (error, needs-configuration and its settings)."""

        def apply(r: dict[str, Any]) -> bool:
            s = next((x for x in self._states if x.source == "marketplace" and x.name == r["name"] and x.version == r["version"]), None)
            if s is None:
                return False
            before = json.dumps([r.get("error"), r.get("status"), r.get("missing"), r.get("settings")])
            if s.error:
                r["error"] = s.error
            else:
                r.pop("error", None)
            if s.status == CLI_SERVER_PLUGIN_NEEDS_CONFIGURATION:
                r["status"] = CLI_SERVER_PLUGIN_NEEDS_CONFIGURATION
                r["missing"] = list(s.missing or [])
            else:
                r.pop("status", None)
                r.pop("missing", None)
            if s.settings:
                r["settings"] = s.settings
            return before != json.dumps([r.get("error"), r.get("status"), r.get("missing"), r.get("settings")])

        if not any([apply(r) for r in records]):
            return
        try:
            current = self.records()
            for r in current:
                apply(r)
            self.save(current)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not write the plugins' status to %s: %s", CLI_SERVER_PLUGINS_FILE, exc)

    def settings_file(self) -> JsonFileConfigStore:
        """The plugins' settings file (``<data dir>/plugin-settings.json``, mode 0600), one store for all."""
        if self._settings_file is None:
            self._settings_file = JsonFileConfigStore(os.path.join(self.data_dir, CLI_PLUGIN_SETTINGS_FILE))
        return self._settings_file

    def _settings_store(self, host: CliServerPluginsHost, provider_id: str) -> Any:
        """While the server keeps its settings in memory, a plugin's go to a file of the data directory (they must
        survive the restart they wait for); a store the host set wins.  Before a config store plugin they wait on the
        default store, which it may replace (else they move to the file after it)."""
        registry = getattr(host.builder, "config_registry", None)
        if registry is None:
            return None
        if getattr(registry, "has_default_store", True) or provider_id in getattr(registry, "provider_stores", {}):
            return None
        if self._config_store_pending:
            self._parked.append(provider_id)
            return None
        return self.settings_file()

    def _settle_config_store(self, host: CliServerPluginsHost) -> None:
        """After the config store plugins: when none set the default store, the parked settings move to the file."""
        if not self._config_store_pending:
            return
        self._config_store_pending = False
        registry = getattr(host.builder, "config_registry", None)
        if registry is not None and not registry.has_default_store:
            for provider_id in self._parked:
                if provider_id not in registry.provider_stores:
                    registry.move_provider(provider_id, self.settings_file())
        self._parked = []

    async def _load_one(self, host: CliServerPluginsHost, candidate: "_Candidate") -> None:
        state = CliServerPluginState(
            candidate.name, candidate.package, candidate.version, CLI_SERVER_PLUGIN_LOADED,
            source=candidate.source, order=candidate.order,
        )
        # Listed first (the plugin's own listing of the package is then ignored)
        host.builder.add_installed_plugin(CliInstalledPlugin(
            id=candidate.package,
            name=candidate.title or short_plugin_name(candidate.package),
            version=candidate.version,
            description=candidate.description,
            icon=candidate.icon,
            source=candidate.source,
        ))
        data_dir = os.path.join(self.data_dir, "plugin-data", re.sub(r"[^A-Za-z0-9._-]", "-", candidate.name))
        os.makedirs(data_dir, exist_ok=True)
        options = candidate.options
        missing: list[list[str]] = []

        async def settings(
            *, settings: Optional[list[Any]] = None, sections: Optional[list[Any]] = None, id: Optional[str] = None,
            title: Optional[str] = None, description: Optional[str] = None, store: Any = None,
        ) -> CliStoredConfigProvider:
            provider_id = id or candidate.name
            defs = sections or [CliConfigSectionDefinition(
                id="settings", title="Settings",
                settings=[s if isinstance(s, CliConfigSettingDefinition) else CliConfigSettingDefinition(**s) for s in settings or []],
            )]
            provider = CliStoredConfigProvider(
                id=provider_id,
                title=title or candidate.title or short_plugin_name(candidate.package),
                description=description,
                sections=with_code_defaults(defs, options),
                store=store if store is not None else self._settings_store(host, provider_id),
            )
            host.builder.add_config_provider(provider)
            state.settings = provider_id
            await host.config.ready()
            return provider

        def needs_configuration(reason: Any) -> None:
            items = [str(r) for r in (reason if isinstance(reason, (list, tuple)) else [reason]) if r]
            missing.append(items or ["settings"])
            return None

        context = CliPluginContext(
            plugin_id=candidate.name, package_name=candidate.package, version=candidate.version, builder=host.builder,
            config=host.config, logger=logging.getLogger(f"qcli.plugin.{candidate.name}"), data_dir=data_dir,
            server_data_dir=self.data_dir, event_bus=host.event_bus, restart=host.restart, settings=settings,
            needs_configuration=needs_configuration, options=dict(options), source=candidate.source,
            services=self._services, settings_file=self.settings_file(),
        )
        try:
            try:
                result = candidate.entry(context)
                if inspect.isawaitable(result):
                    result = await result
            except CliPluginNeedsConfiguration as exc:
                needs_configuration(exc.missing)
                result = None
            # A plugin waiting for its settings registers nothing, whatever it returned
            if not missing:
                self._register(host, result)
        except BaseException:
            host.builder.remove_installed_plugin(candidate.package)
            raise
        update = getattr(host.builder, "update_installed_plugin", None)
        if missing:
            state.status, state.missing = CLI_SERVER_PLUGIN_NEEDS_CONFIGURATION, missing[-1]
            if callable(update):
                update(candidate.package, status=CLI_SERVER_PLUGIN_NEEDS_CONFIGURATION, missing=tuple(missing[-1]))
            hint = f" (config set {state.settings}.<section>.<key> <value>, then restart)" if state.settings else ""
            logger.warning("%s needs configuration: %s%s", candidate.name, ", ".join(missing[-1]), hint)
        else:
            logger.info("loaded %s (%s %s, %s)", candidate.name, candidate.package, candidate.version, candidate.source)
        if state.settings and callable(update):
            update(candidate.package, settings=state.settings)
        self._states.append(state)

    @staticmethod
    def _register(host: CliServerPluginsHost, result: Any) -> None:
        if result is None:
            return
        if _is_mountable(result):
            # Mounting adds its ``module`` (commands) too
            host.mount(result)
        elif _is_module(result):
            host.builder.add_module(result)
        else:
            raise ValueError("create_plugin returned neither a plugin result (prefix, router) nor a module (name, processors)")


class CliPluginServices:
    """What plugins share with each other (``context.services``): ``set("users", plugin)``, ``get("users")``."""

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}

    def get(self, name: str) -> Any:
        return self._values.get(name)

    def set(self, name: str, value: Any) -> None:
        self._values[name] = value

    def has(self, name: str) -> bool:
        return name in self._values


@dataclass
class _Candidate:
    name: str
    package: str
    version: str
    source: str
    entry: Any
    order: int
    options: dict[str, Any]
    title: Optional[str] = None
    icon: Optional[str] = None
    description: Optional[str] = None
    record: Optional[dict[str, Any]] = None


def _order_of(*values: Any) -> int:
    for v in values:
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf"):
            return int(v)
    return CLI_PLUGIN_LOAD_ORDER.DEFAULT


def _entry_of(entry: Any) -> tuple[Any, Any]:
    """``create_plugin`` and its module from an entry module or the function."""
    if inspect.ismodule(entry):
        fn = getattr(entry, "create_plugin", None)
        return (fn if callable(fn) else None), entry
    if callable(entry):
        return entry, sys.modules.get(getattr(entry, "__module__", "") or "")
    return None, None


def _info_of(value: Any) -> Optional[CliPluginEntryInfo]:
    if isinstance(value, CliPluginEntryInfo):
        info = value
    elif isinstance(value, dict) and isinstance(value.get("id"), str) and isinstance(value.get("package"), str):
        info = CliPluginEntryInfo(**{k: value.get(k) for k in ("id", "package", "version", "title", "icon", "description")})
    else:
        return None
    if not is_valid_server_plugin_name(info.id) or not is_valid_server_plugin_package(info.package):
        raise ValueError(f"invalid plugin info: {info.id} ({info.package})")
    return info


def _is_config_value(value: Any) -> bool:
    """A value a setting can take from code options (JSON data; not a pool, a store or a function)."""
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, (int, float)):
        return value == value and abs(value) != float("inf")
    if isinstance(value, (list, tuple)):
        return all(_is_config_value(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_config_value(v) for k, v in value.items())
    return False


def with_code_defaults(sections: list[Any], options: dict[str, Any]) -> list[Any]:
    """The sections with the code's options as the defaults of the settings they name (source ``code``); options that
    are not settings, or not JSON data, are left to the plugin."""
    if not options:
        return list(sections)
    out = []
    for section in sections:
        settings = []
        for s in section.settings:
            value = options.get(s.key)
            if s.key in options and value is not None and _is_config_value(value) and s.mutable:
                s = replace(s, default=list(value) if isinstance(value, tuple) else value, source="code")
            settings.append(s)
        out.append(replace(section, settings=settings))
    return out


def importlib_invalidate() -> None:
    import importlib

    importlib.invalidate_caches()
