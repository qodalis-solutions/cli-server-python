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

The same entry serves a plugin added in code: ``builder.add_plugin(create_plugin, options={"defaultLocation": "Paris"})``
runs it at startup with ``context.options``; ``context.settings`` then takes those options as the defaults (source
``code``) and saved settings override them, so the dashboard shows the same settings either way.

A setting marked ``required`` without a value makes ``context.settings`` raise :class:`CliPluginNeedsConfiguration`:
the plugin is listed as ``needs-configuration`` (its settings registered, so they can be filled in) and loads at the
next start.  Plugins load in their declared order (:func:`cli_server_plugin`): authentication first, then config
stores, then cores (data explorer), plugins, and the sources of a core last.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

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

#: Load order (lower first): authentication (the users plugin's middleware), config stores, cores other plugins
#: extend (data explorer), plugins, then sources of a core.
CLI_PLUGIN_ORDER_AUTH = 10
CLI_PLUGIN_ORDER_CONFIG_STORE = 20
CLI_PLUGIN_ORDER_CORE = 30
CLI_PLUGIN_ORDER_DEFAULT = 50
CLI_PLUGIN_ORDER_SOURCE = 70


class CliPluginNeedsConfiguration(Exception):
    """Raised by ``context.settings`` (or a plugin) when required settings have no value."""

    def __init__(self, missing: list[str], message: Optional[str] = None) -> None:
        self.missing = list(missing)
        super().__init__(message or f"needs configuration: {', '.join(self.missing)}")


def cli_server_plugin(order: int = CLI_PLUGIN_ORDER_DEFAULT) -> Callable[[Any], Any]:
    """Declares a ``create_plugin``'s load order::

        @cli_server_plugin(order=CLI_PLUGIN_ORDER_SOURCE)
        async def create_plugin(context): ...
    """

    def mark(fn: Any) -> Any:
        fn.qcli_plugin_order = order
        return fn

    return mark


def cli_server_plugin_order(entry: Any) -> int:
    """An entry's declared load order (default :data:`CLI_PLUGIN_ORDER_DEFAULT`)."""
    order = getattr(entry, "qcli_plugin_order", CLI_PLUGIN_ORDER_DEFAULT)
    return order if isinstance(order, int) else CLI_PLUGIN_ORDER_DEFAULT


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
    #: The code options are the defaults (source ``code``); raises :class:`CliPluginNeedsConfiguration` when a
    #: ``required`` setting has no value (``check=False`` returns the provider anyway).
    settings: Callable[..., Awaitable[Any]]
    #: The options of a plugin added in code (``builder.add_plugin(entry, options=...)``): ``{key: value}`` for the
    #: section ``settings``, or ``{section: {key: value}}``; empty for a plugin installed from the marketplace.
    options: dict[str, Any] = field(default_factory=dict)
    #: ``marketplace`` or ``code``.
    source: str = "marketplace"


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
    missing: Optional[list[str]] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name, "package": self.package, "version": self.version, "status": self.status,
            "source": self.source,
        }
        if self.error:
            out["error"] = self.error
        if self.missing:
            out["missing"] = list(self.missing)
        return out


__all__ = [
    "CLI_PLUGINS_INSTALL_PERMISSION",
    "CLI_PLUGIN_ORDER_AUTH",
    "CLI_PLUGIN_ORDER_CONFIG_STORE",
    "CLI_PLUGIN_ORDER_CORE",
    "CLI_PLUGIN_ORDER_DEFAULT",
    "CLI_PLUGIN_ORDER_SOURCE",
    "CLI_SERVER_PLUGINS_DIR",
    "CLI_SERVER_PLUGINS_FILE",
    "CLI_SERVER_PLUGIN_ENTRY_GROUP",
    "CLI_SERVER_PLUGIN_FAILED",
    "CLI_SERVER_PLUGIN_KIND",
    "CLI_SERVER_PLUGIN_LOADED",
    "CLI_SERVER_PLUGIN_NEEDS_CONFIGURATION",
    "CLI_SERVER_PLUGIN_SKIPPED",
    "CliPluginContext",
    "CliPluginNeedsConfiguration",
    "cli_server_plugin",
    "cli_server_plugin_order",
    "CliPluginEntry",
    "CliServerPluginState",
]
