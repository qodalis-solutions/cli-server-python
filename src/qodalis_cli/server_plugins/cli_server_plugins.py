"""Server plugins installed at runtime (the admin plugin's server marketplace installs them): ``<data dir>/server-plugins``
holds the packages (``pip install --target``) and ``installed.json``; at startup the directory is added to ``sys.path``
after the server's own packages and each listed package's ``qodalis_cli.plugins`` entry point is called with a
:class:`CliPluginContext`.  A plugin that fails is logged and its error kept in ``installed.json``: startup goes on.
The same ``installed.json`` and states as the Node server."""

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
    CliPluginNeedsConfiguration,
    CliServerPluginState,
    cli_server_plugin_order,
)

logger = logging.getLogger("qodalis_cli.server_plugins")

#: The server's data directory unless ``data_dir`` is given: ``./qcli-data`` (from the working directory).
DEFAULT_DATA_DIR = "qcli-data"

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

    def add(self, entry: Any, options: Optional[dict[str, Any]] = None, name: Optional[str] = None) -> None:
        """A plugin added in code (``builder.add_plugin``): its ``create_plugin`` runs at startup like a marketplace
        plugin's, in the declared order, with ``options`` as its settings' defaults (source ``code``)."""
        if not callable(entry):
            raise TypeError("a server plugin entry must be a function create_plugin(context)")
        if self._loaded:
            raise RuntimeError("server plugins are already loaded: add them before the server starts")
        package = _distribution_of(entry)
        plugin_id = name or _plugin_id_of(entry, package)
        if not is_valid_server_plugin_name(plugin_id):
            raise ValueError(f"invalid server plugin name: {plugin_id!r}")
        if any(c.name == plugin_id for c in self._code):
            raise ValueError(f"server plugin {plugin_id} is added twice")
        version = "0.0.0"
        if package is not None:
            try:
                version = metadata.version(package)
            except metadata.PackageNotFoundError:  # pragma: no cover
                pass
        self._code.append(_Candidate(
            name=plugin_id, package=package or plugin_id, version=version, source="code", entry=entry,
            options=dict(options or {}), record={"name": plugin_id, "package": package or plugin_id, "version": version},
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

    def _resolve(self, host: CliServerPluginsHost, record: dict[str, Any]) -> "_Candidate":
        """A marketplace record's entry (raises _Skipped or an error)."""
        package = record["package"]
        normalized = normalize_distribution(package)
        installed = getattr(host.builder, "installed_plugins", None) or []
        if any(normalize_distribution(p.id) == normalized and getattr(p, "source", None) != "marketplace" for p in installed):
            raise _Skipped("already installed by the host")
        if any(normalize_distribution(c.package) == normalized for c in self._code):
            raise _Skipped("already added in code")
        own = self._host_distribution(package)
        if own is not None:
            # Its modules would be imported from the server's environment, not from the plugins directory
            raise _Skipped(f"installed in the server's environment ({package} {own.version})")
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
        if not origin.startswith(os.path.realpath(self.dir) + os.sep):
            raise ValueError(f"the entry {entry.value} is outside the plugins directory")
        if not callable(create_plugin):
            raise ValueError(f"the entry {entry.value} is not a function create_plugin(context)")
        summary = dist.metadata.get("Summary") if dist.metadata else None
        return _Candidate(
            name=record["name"], package=package, version=record["version"], source="marketplace",
            entry=create_plugin, options={}, record=record, summary=summary,
        )

    async def _load_all(self, host: CliServerPluginsHost) -> None:
        records = self.records() if self.enabled else []
        candidates: list[_Candidate] = list(self._code)
        if records:
            self._add_to_path()
        seen: set[str] = {normalize_distribution(c.package) for c in self._code}
        for record in records:
            state = CliServerPluginState(record["name"], record["package"], record["version"], "loaded")
            try:
                if normalize_distribution(record["package"]) in seen and not any(
                    normalize_distribution(c.package) == normalize_distribution(record["package"]) for c in self._code
                ):
                    raise _Skipped("listed twice")
                candidates.append(self._resolve(host, record))
                seen.add(normalize_distribution(record["package"]))
                continue
            except _Skipped as exc:
                state.status, state.error = CLI_SERVER_PLUGIN_SKIPPED, _first_line(exc)
                logger.warning("%s (%s %s) not loaded: %s", record["name"], record["package"], record["version"], state.error)
            except BaseException as exc:  # noqa: BLE001 - a plugin never stops the server (SyntaxError, SystemExit, ...)
                if isinstance(exc, KeyboardInterrupt):
                    raise
                state.status, state.error = CLI_SERVER_PLUGIN_FAILED, _first_line(exc)
                logger.error("%s (%s %s) not loaded: %s", record["name"], record["package"], record["version"], state.error)
            self._states.append(state)

        # The declared order (auth, config stores, cores, plugins, sources); code plugins first within an order
        candidates.sort(key=lambda c: cli_server_plugin_order(c.entry))
        for candidate in candidates:
            state = CliServerPluginState(
                candidate.name, candidate.package, candidate.version, CLI_SERVER_PLUGIN_LOADED, source=candidate.source,
            )
            try:
                await self._run(host, candidate)
                logger.info("loaded %s (%s %s, %s)", candidate.name, candidate.package, candidate.version, candidate.source)
            except CliPluginNeedsConfiguration as exc:
                state.status, state.error, state.missing = CLI_SERVER_PLUGIN_NEEDS_CONFIGURATION, _first_line(exc), exc.missing
                logger.warning("%s (%s %s) needs configuration: %s", candidate.name, candidate.package, candidate.version,
                               ", ".join(exc.missing))
            except BaseException as exc:  # noqa: BLE001 - a plugin never stops the server
                if isinstance(exc, KeyboardInterrupt):
                    raise
                state.status, state.error = CLI_SERVER_PLUGIN_FAILED, _first_line(exc)
                logger.error("%s (%s %s) not loaded: %s", candidate.name, candidate.package, candidate.version, state.error)
            self._states.append(state)
        if candidates:
            try:
                host.after_load()
            except Exception as exc:  # noqa: BLE001
                logger.error("wiring the server plugins failed: %s", exc)
        if records and any(
            r.get("error") != next((s.error for s in self._states if s.name == r["name"] and s.source == "marketplace"), None)
            for r in records
        ):
            # The errors in the installed list, for the dashboard (and cleared once a plugin loads)
            try:
                current = self.records()
                for r in current:
                    s = next((x for x in self._states if x.name == r["name"] and x.version == r["version"]
                              and x.source == "marketplace"), None)
                    if s is None:
                        continue
                    if s.error:
                        r["error"] = s.error
                    else:
                        r.pop("error", None)
                self.save(current)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not write the errors to %s: %s", CLI_SERVER_PLUGINS_FILE, exc)

    def _host_distribution(self, package: str) -> Any:
        """The package as the server's own environment has it (outside the plugins directory), or ``None``."""
        paths = [p for p in sys.path if os.path.abspath(p or ".") != self.dir]
        return next(iter(metadata.distributions(name=package, path=paths)), None)

    async def _run(self, host: CliServerPluginsHost, candidate: "_Candidate") -> None:
        from ..models.cli_installed_plugin import CliInstalledPlugin, short_plugin_name

        record = candidate.record
        package = candidate.package
        if candidate.source == "marketplace":
            # Listed first, as installed from the marketplace (the plugin's own listing of the package is then ignored)
            host.builder.add_installed_plugin(CliInstalledPlugin(
                id=package,
                name=record.get("title") or short_plugin_name(package),
                version=candidate.version,
                description=record.get("description") or candidate.summary or None,
                icon=record.get("icon"),
                source="marketplace",
            ))
        data_dir = os.path.join(self.data_dir, "plugin-data", candidate.name)
        os.makedirs(data_dir, exist_ok=True)
        options = candidate.options

        async def settings(
            *, settings: Optional[list[Any]] = None, sections: Optional[list[Any]] = None, id: Optional[str] = None,
            title: Optional[str] = None, description: Optional[str] = None, store: Any = None, check: bool = True,
        ) -> CliStoredConfigProvider:
            defs = sections or [CliConfigSectionDefinition(
                id="settings", title="Settings",
                settings=[s if isinstance(s, CliConfigSettingDefinition) else CliConfigSettingDefinition(**s) for s in settings or []],
            )]
            defs = with_code_options(defs, options)
            provider = CliStoredConfigProvider(
                id=id or candidate.name,
                title=title or record.get("title") or short_plugin_name(package),
                description=description,
                sections=defs,
                store=store,
            )
            host.builder.add_config_provider(provider)
            await host.config.ready()
            missing = provider.missing()
            if check and missing:
                raise CliPluginNeedsConfiguration(missing)
            return provider

        context = CliPluginContext(
            plugin_id=candidate.name, package_name=package, version=candidate.version, builder=host.builder,
            config=host.config, logger=logging.getLogger(f"qcli.plugin.{candidate.name}"), data_dir=data_dir,
            server_data_dir=self.data_dir, event_bus=host.event_bus, restart=host.restart, settings=settings,
            options=dict(options), source=candidate.source,
        )
        try:
            result = candidate.entry(context)
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                return
            if _is_mountable(result):
                host.mount(result)
                # A plugin result may bring its commands too (``module`` or ``modules``)
                extra = getattr(result, "modules", None) or ([result.module] if getattr(result, "module", None) else [])
                for module in extra:
                    if _is_module(module):
                        host.builder.add_module(module)
            elif _is_module(result):
                host.builder.add_module(result)
            else:
                raise ValueError(
                    "create_plugin returned neither a plugin result (prefix, router) nor a module (name, processors)",
                )
        except BaseException:
            if candidate.source == "marketplace":
                host.builder.remove_installed_plugin(package)
            raise


@dataclass
class _Candidate:
    name: str
    package: str
    version: str
    source: str
    entry: Any
    options: dict[str, Any]
    record: dict[str, Any]
    summary: Optional[str] = None


def with_code_options(sections: list[Any], options: dict[str, Any]) -> list[Any]:
    """The sections with the code options as defaults (source ``code``): ``{key: value}`` for the first section or
    ``{section: {key: value}}``.  Unknown keys raise (a typo in code should not pass silently)."""
    if not options:
        return list(sections)
    by_section: dict[str, dict[str, Any]] = {}
    ids = {s.id for s in sections}
    for key, value in options.items():
        if key in ids and isinstance(value, dict):
            by_section.setdefault(key, {}).update(value)
        else:
            by_section.setdefault(sections[0].id, {})[key] = value
    out = []
    for section in sections:
        values = by_section.pop(section.id, {})
        known = {s.key for s in section.settings}
        unknown = [k for k in values if k not in known]
        if unknown:
            raise ValueError(f"unknown plugin options: {', '.join(f'{section.id}.{k}' for k in unknown)}")
        settings = [
            replace(s, default=values[s.key], source="code") if s.key in values and values[s.key] is not None else s
            for s in section.settings
        ]
        out.append(replace(section, settings=settings))
    if by_section:
        raise ValueError(f"unknown plugin option sections: {', '.join(by_section)}")
    return out


def _distribution_of(entry: Any) -> Optional[str]:
    """The installed distribution that provides ``entry``'s module (``None`` when not found)."""
    module = getattr(entry, "__module__", None) or ""
    top = module.split(".")[0]
    try:
        dists = metadata.packages_distributions().get(top) or []
    except Exception:  # noqa: BLE001
        dists = []
    return dists[0] if dists else None


def _plugin_id_of(entry: Any, package: Optional[str]) -> str:
    """The plugin's id: its entry point's name in the package, else the package's short name."""
    if package is not None:
        try:
            for ep in metadata.distribution(package).entry_points.select(group=CLI_SERVER_PLUGIN_ENTRY_GROUP):
                if ep.value.split(":")[0] == entry.__module__ and ep.value.split(":")[-1] == entry.__name__:
                    return ep.name
        except Exception:  # noqa: BLE001
            pass
        from ..models.cli_installed_plugin import short_plugin_name

        return re.sub(r"[^a-z0-9._-]", "-", short_plugin_name(package).lower())
    return re.sub(r"[^a-z0-9._-]", "-", (getattr(entry, "__module__", "plugin").split(".")[0]).lower())


def importlib_invalidate() -> None:
    import importlib

    importlib.invalidate_caches()
