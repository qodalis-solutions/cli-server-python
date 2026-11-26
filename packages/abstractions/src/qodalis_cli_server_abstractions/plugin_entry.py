"""Server plugins installed at runtime (from the marketplace): the entry point a plugin package declares and the context
the server hands it.  The same contract on every platform: Node (``qodalis.entry`` exporting ``createPlugin(context)``),
Python (entry point group ``qodalis_cli.plugins``) and .NET (``[assembly: CliPlugin]``).

A Python package declares it in its ``pyproject.toml``::

    [project.entry-points."qodalis_cli.plugins"]
    weather = "qodalis_cli_weather.plugin:create_plugin"

and the function takes a :class:`CliPluginContext` (sync or async)::

    async def create_plugin(context):
        settings = await context.settings(title="Weather", settings=[
            CliConfigSettingDefinition(key="defaultLocation", type="string", default="London"),
        ])
        return WeatherModule(default_location=lambda: settings.value("settings", "defaultLocation"))

It returns a plugin result the server mounts (anything with ``prefix`` and ``router``, like the official plugins'
builders give), a module (``name`` and ``processors``), or ``None`` when it registered everything on the builder.

The same entry serves a plugin added in code: ``builder.add_plugin(entry, options)`` calls ``create_plugin`` with
``context.options`` (the code's values: the defaults of its settings, origin ``code``; a saved setting overrides
them) and ``context.source`` ``code``.  Both install paths show the same settings on the dashboard's Config page.
The entry module names its plugin (``plugin = CliPluginEntryInfo(id="weather", package="qodalis-cli-server-plugin-
weather")``) and may set ``order`` (:data:`CLI_PLUGIN_LOAD_ORDER`) to load before or after the others.

A plugin whose required settings (``required=True``) have no value returns
``context.needs_configuration(provider.missing_required())``: it registers nothing, its state is
``needs-configuration``, and it runs once they are saved and the server restarts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

#: The entry point group of server plugins installed at runtime.
CLI_SERVER_PLUGIN_ENTRY_GROUP = "qodalis_cli.plugins"
#: The kind of a server plugin in the marketplace.
CLI_SERVER_PLUGIN_KIND = "server"
#: Where a server keeps its runtime-installed plugins, below its data directory.
CLI_SERVER_PLUGINS_DIR = "server-plugins"
#: The list of installed plugins in that directory.
CLI_SERVER_PLUGINS_FILE = "installed.json"
#: The permission to install, update and remove server plugins.
CLI_PLUGINS_INSTALL_PERMISSION = "plugins:install"

#: The states of a server plugin after startup.
CLI_SERVER_PLUGIN_LOADED = "loaded"
CLI_SERVER_PLUGIN_FAILED = "failed"
CLI_SERVER_PLUGIN_SKIPPED = "skipped"
CLI_SERVER_PLUGIN_NEEDS_CONFIGURATION = "needs-configuration"


class CLI_PLUGIN_LOAD_ORDER:  # noqa: N801 - a constant namespace, as on Node and .NET
    """When an entry loads (its module's ``order``, lower first; code-added and installed plugins sorted together,
    code first on a tie): ``AUTH`` (the sign-in: users; its stores at ``AUTH - 5``), ``CONFIG_STORE`` (config stores:
    the server's default store is replaced and the providers already using it move to the new one), ``CORE`` (the
    databases, the data explorer before its sources), ``DEFAULT``, ``SOURCE`` (data explorer sources)."""

    AUTH = 10
    CONFIG_STORE = 20
    CORE = 30
    DEFAULT = 50
    SOURCE = 70


@dataclass
class CliPluginEntryInfo:
    """The plugin an entry belongs to (``plugin = CliPluginEntryInfo(...)`` in the entry's module), for
    ``builder.add_plugin`` (the marketplace's record says it for an installed one)."""

    #: The marketplace entry's id (``users``): the default id of its settings provider.
    id: str
    #: The PyPI distribution.
    package: str
    version: Optional[str] = None
    title: Optional[str] = None
    icon: Optional[str] = None
    description: Optional[str] = None


class ICliPluginServices(Protocol):
    """What plugins share with each other: the users plugin (``users``), the public API, ..."""

    def get(self, name: str) -> Any: ...

    def set(self, name: str, value: Any) -> None: ...

    def has(self, name: str) -> bool: ...


class CliPluginNeedsConfiguration(Exception):
    """Raised by an entry instead of returning ``context.needs_configuration(missing)``: the same state."""

    def __init__(self, missing: list[str] | str, message: Optional[str] = None) -> None:
        self.missing = [missing] if isinstance(missing, str) else list(missing)
        super().__init__(message or f"needs configuration: {', '.join(self.missing)}")


@dataclass
class CliPluginContext:
    """What the server hands a plugin's ``create_plugin``."""

    #: The marketplace entry's id (``weather``).
    plugin_id: str
    #: The PyPI distribution (``qodalis-cli-server-plugin-weather``).
    package_name: str
    #: The installed version.
    version: str
    #: The server's builder (``CliBuilder``): ``add_module``, ``add_processor``, ``add_config_provider``,
    #: ``set_file_storage_provider``, ``add_data_explorer_provider``, ...
    builder: Any
    #: The server's configuration service.
    config: Any
    #: Logs under ``qcli.plugin.<id>``.
    logger: Any
    #: A directory of the plugin's own (``<data dir>/plugin-data/<id>``), created; kept across updates.
    data_dir: str
    #: The server's data directory (``./qcli-data`` by default).
    server_data_dir: str
    #: The server's event bus.
    event_bus: Any
    #: The server's restart service.
    restart: Any
    #: ``await context.settings(title=..., settings=[...] | sections=[...], id=..., description=..., store=...)``:
    #: registers the plugin's settings as a ``CliStoredConfigProvider`` (default id: the plugin's id, one section
    #: ``settings``) and returns it once its stored values are loaded: read them with ``provider.value(section, key)``.
    #: A setting whose key is in ``options`` takes that value as its default (origin ``code``).  While the server
    #: keeps its settings in memory, a plugin's are kept in ``<data dir>/plugin-settings.json`` (they must survive the
    #: restart they wait for); a store the host set wins.
    settings: Callable[..., Awaitable[Any]]
    #: ``return context.needs_configuration(provider.missing_required())``: marks the plugin ``needs-configuration``
    #: with the settings it still needs (``section.key``, or a reason); what it returns is not registered.
    needs_configuration: Callable[[Any], None] = lambda missing: None
    #: The options the host added the plugin with (``builder.add_plugin(entry, options)``; empty from the
    #: marketplace).  Values that are not settings (a pool, a store, a function) are read from here directly.
    options: dict[str, Any] = field(default_factory=dict)
    #: ``marketplace`` or ``code``.
    source: str = "marketplace"
    #: What plugins share (``ICliPluginServices``).
    services: Any = None
    #: The plugins' settings file (``<data dir>/plugin-settings.json``, a ``JsonFileConfigStore``): for settings that
    #: must not live in the server's config store (the config-redis plugin's own URL).
    settings_file: Any = None
    #: The server's notifications (``ICliNotifications``): ``notify({"key", "kind", "severity", "title", "message"})``
    #: tells the dashboard's users, ``resolve(key)`` removes it once the condition ended.  ``None`` on servers before it.
    notifications: Any = None


#: ``create_plugin(context)``: sync or async, returns a plugin result, a module or ``None``.
CliPluginEntry = Callable[[CliPluginContext], Any]


@dataclass
class CliServerPluginState:
    """A plugin the server tried to load at startup: ``loaded``, ``failed``, ``skipped`` or ``needs-configuration``
    (``error`` why, ``missing`` the required settings without a value)."""

    name: str
    package: str
    version: str
    status: str
    error: Optional[str] = None
    #: ``marketplace`` or ``code``.
    source: str = "marketplace"
    #: ``needs-configuration``: the settings it needs (``section.key``) or the reason.
    missing: Optional[list[str]] = None
    #: The id of its settings provider, when it registered one.
    settings: Optional[str] = None
    order: int = 50

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name, "package": self.package, "version": self.version, "status": self.status,
            "source": self.source, "order": self.order,
        }
        if self.error:
            out["error"] = self.error
        if self.missing:
            out["missing"] = list(self.missing)
        if self.settings:
            out["settings"] = self.settings
        return out


__all__ = [
    "CLI_PLUGINS_INSTALL_PERMISSION",
    "CLI_PLUGIN_LOAD_ORDER",
    "CLI_SERVER_PLUGINS_DIR",
    "CLI_SERVER_PLUGINS_FILE",
    "CLI_SERVER_PLUGIN_ENTRY_GROUP",
    "CLI_SERVER_PLUGIN_FAILED",
    "CLI_SERVER_PLUGIN_KIND",
    "CLI_SERVER_PLUGIN_LOADED",
    "CLI_SERVER_PLUGIN_NEEDS_CONFIGURATION",
    "CLI_SERVER_PLUGIN_SKIPPED",
    "CliPluginContext",
    "CliPluginEntry",
    "CliPluginEntryInfo",
    "CliPluginNeedsConfiguration",
    "CliServerPluginState",
    "ICliPluginServices",
]
