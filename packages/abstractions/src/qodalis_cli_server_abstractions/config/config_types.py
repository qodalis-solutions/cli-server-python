"""Config providers: the server's configuration, read and changed through
providers that the server, its plugins and the host application register.
The same contract and JSON in the Node, .NET and Python servers
(cli-server-dashboard#61)."""

from __future__ import annotations

import abc
import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Mapping, Optional, Sequence, Union

#: The type of a setting; it decides the input the dashboard shows and how a value is validated.
CliConfigEntryType = Literal["string", "number", "boolean", "string[]", "enum", "secret", "json", "duration", "url"]

#: Every entry type.
CLI_CONFIG_ENTRY_TYPES: tuple[str, ...] = (
    "string", "number", "boolean", "string[]", "enum", "secret", "json", "duration", "url",
)

#: Where a setting's current value comes from: its ``default``, a configuration
#: ``file``, an ``environment`` variable, the ``database``, or a change made at
#: ``runtime`` (kept in memory).
CliConfigSource = Literal["default", "file", "environment", "database", "runtime"]

#: What kind of store keeps the writable settings: in ``memory`` (lost on
#: restart), a JSON ``file``, a ``database``, ``redis``, or a ``custom`` store.
CliConfigStoreKind = Literal["memory", "file", "database", "redis", "custom"]

#: Where a provider's settings come from: ``store`` (changes are saved to its
#: config store, the provider view's ``store``), ``runtime`` (the running process), ``options`` (the options
#: the server was created with), ``environment`` (environment variables),
#: ``configuration`` (the host's configuration) or ``custom``.
CliConfigStorageKind = Literal["store", "runtime", "options", "environment", "configuration", "custom"]


@dataclass
class CliConfigStoreDescription:
    """Where changes are saved: what a store says about itself
    (``ICliConfigStore.describe``), as each stored provider's ``store`` and
    the top-level ``defaultStore`` of ``GET /api/v1/qcli/config``.
    ``detail`` names the file (relative to the app), the database and table,
    or the Redis host and key prefix, never credentials or a connection string."""

    kind: str
    #: ``In memory``, ``JSON file``, ``Database``, ...
    title: str
    #: ``qcli-demo-config.json``, ``PostgreSQL database qcli, table QcliConfig``,
    #: ``cache:6379/0, prefix qcli:config``; ``''`` when nothing to add.
    detail: str = ""
    #: Changes are kept across restarts.
    persistent: bool = False

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape."""
        return {"kind": self.kind, "title": self.title, "detail": self.detail, "persistent": self.persistent}


@dataclass
class CliConfigProviderStorage:
    """Where a provider reads its settings from and saves changes to (``storage`` of a provider view)."""

    kind: str
    description: str = ""
    #: The origins in the order they apply (later ones override earlier
    #: ones): ``Server options``, ``Environment variables``, ...; ``[]`` when none.
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape (``sources`` always present)."""
        return {"kind": self.kind, "description": self.description, "sources": list(self.sources)}


#: A setting's value: JSON (a ``secret`` is a string, a ``duration`` a string such as ``30m``).
CliConfigValue = Any

#: Per-key errors: the key's message (``{"maxAge": "must be at least 1m"}``).
CliConfigErrors = dict[str, str]


@dataclass
class CliConfigValidation:
    """Validation rules of a setting (checked by the server on every change)."""

    #: ``number``/``duration`` (milliseconds): the smallest value; ``string``/``secret``/``url``:
    #: the shortest length; ``string[]``: the fewest items.
    min: Optional[float] = None
    #: Same as ``min``, the largest.
    max: Optional[float] = None
    #: A regular expression the value must match (searched, as in JavaScript); each item of a ``string[]``.
    pattern: Optional[str] = None
    #: The allowed values of an ``enum``, or of each item of a ``string[]`` / ``string``.
    options: Optional[list[str]] = None
    #: The value may not be empty (``None``, ``''`` or ``[]``).
    required: bool = False

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape (unset rules omitted)."""
        out: dict[str, Any] = {}
        if self.min is not None:
            out["min"] = _number(self.min)
        if self.max is not None:
            out["max"] = _number(self.max)
        if self.pattern is not None:
            out["pattern"] = self.pattern
        if self.options is not None:
            out["options"] = list(self.options)
        if self.required:
            out["required"] = True
        return out

    @staticmethod
    def of(value: Any) -> Optional["CliConfigValidation"]:
        """A validation from a :class:`CliConfigValidation` or a mapping (``None`` stays ``None``)."""
        if value is None or isinstance(value, CliConfigValidation):
            return value
        if isinstance(value, Mapping):
            return CliConfigValidation(
                min=value.get("min"),
                max=value.get("max"),
                pattern=value.get("pattern"),
                options=list(value["options"]) if value.get("options") is not None else None,
                required=bool(value.get("required", False)),
            )
        raise TypeError(f"invalid config validation: {value!r}")


def _number(value: float) -> Union[int, float]:
    return int(value) if isinstance(value, float) and value.is_integer() else value


#: Marks a field a provider left out (``value`` of a secret it does not return).
_UNSET: Any = object()


@dataclass
class CliConfigEntry:
    """One setting, as a provider describes it.  The server fills the
    defaults (``label`` = ``key``, ``description`` = ``''``, ``source`` =
    ``default``, ``mutable`` / ``requires_restart`` / ``sensitive`` = False)
    and never returns the value or default of a ``sensitive`` entry (every
    ``secret`` is sensitive): it answers ``value: null`` and ``isSet``."""

    key: str
    type: str
    label: Optional[str] = None
    description: Optional[str] = None
    #: The current value.  Providers may leave a secret's value out and set ``is_set`` instead.
    value: CliConfigValue = None
    #: The value the setting has when nothing sets it (``None`` when none).
    default: CliConfigValue = None
    source: Optional[str] = None
    #: The setting can be changed (``PATCH``, ``config set``, ``config_set``).
    mutable: bool = False
    #: A change takes effect only after the server restarts.
    requires_restart: bool = False
    #: The value is never returned (always true for a ``secret``).
    sensitive: bool = False
    #: For a sensitive entry whose ``value`` the provider leaves out: whether one is set.
    is_set: Optional[bool] = None
    validation: Optional[CliConfigValidation] = None
    #: A hint for a richer input: ``tags``, ``cron``, ``code``, ``origins``, ``textarea``, ...
    editor: Optional[str] = None
    #: Must have a value for its owner to work (a server plugin without it loads as ``needs-configuration``).
    required: bool = False

    @staticmethod
    def of(value: Any) -> "CliConfigEntry":
        """An entry from a :class:`CliConfigEntry` or a mapping (camelCase or snake_case keys)."""
        if isinstance(value, CliConfigEntry):
            return value
        if isinstance(value, Mapping):
            return CliConfigEntry(
                key=value["key"],
                type=value["type"],
                label=value.get("label"),
                description=value.get("description"),
                value=value.get("value"),
                default=value.get("default"),
                source=value.get("source"),
                mutable=bool(value.get("mutable", False)),
                requires_restart=bool(value.get("requires_restart", value.get("requiresRestart", False))),
                sensitive=bool(value.get("sensitive", False)),
                is_set=value.get("is_set", value.get("isSet")),
                validation=CliConfigValidation.of(value.get("validation")),
                editor=value.get("editor"),
                required=bool(value.get("required", False)),
            )
        raise TypeError(f"invalid config entry: {value!r}")


@dataclass
class CliConfigSection:
    """A group of settings of a provider, changed together (one version)."""

    #: Unique within the provider: letters, digits, ``_``, ``-`` and ``.``.
    id: str
    entries: list[CliConfigEntry] = field(default_factory=list)
    title: Optional[str] = None
    description: Optional[str] = None
    #: The section's version (an opaque string): a change made against an older
    #: one gets 409.  Writable sections only.
    version: Optional[str] = None
    #: Keys not in ``entries`` may be added (and removed with ``None``): a free
    #: key/value section such as ``custom``.
    allow_custom_keys: bool = False

    @staticmethod
    def of(value: Any) -> "CliConfigSection":
        """A section from a :class:`CliConfigSection` or a mapping."""
        if isinstance(value, CliConfigSection):
            return value
        if isinstance(value, Mapping):
            return CliConfigSection(
                id=value["id"],
                entries=[CliConfigEntry.of(e) for e in value.get("entries") or []],
                title=value.get("title"),
                description=value.get("description"),
                version=value.get("version"),
                allow_custom_keys=bool(value.get("allow_custom_keys", value.get("allowCustomKeys", False))),
            )
        raise TypeError(f"invalid config section: {value!r}")


@dataclass(frozen=True)
class CliConfigCaller:
    """The signed-in caller of a config read or change."""

    user_id: Optional[str] = None
    user_name: Optional[str] = None
    permissions: tuple[str, ...] = ()


#: Where a config request comes from: the HTTP API, the terminal (``config`` command) or MCP.
CliConfigVia = Literal["api", "terminal", "mcp"]


@dataclass
class CliConfigContext:
    """Who reads or changes the configuration, and what they may do."""

    #: The signed-in caller; ``None`` on a server without authentication.
    caller: Optional[CliConfigCaller] = None
    #: Where the request comes from (``api``, ``terminal``, ``mcp``).
    via: Optional[str] = None
    #: Decides :meth:`has_permission`; default: the caller's permissions
    #: (``resource:action`` matching), every permission without a caller.
    permission_check: Optional[Callable[[str], bool]] = field(default=None, repr=False)

    def has_permission(self, permission: str) -> bool:
        """True when the caller holds the permission (always true on a server without authentication)."""
        if self.permission_check is not None:
            return bool(self.permission_check(permission))
        return self.caller is None or has_cli_permission(self.caller.permissions, permission)


class ICliConfigProvider(abc.ABC):
    """A config provider: a named group of sections.  Read-only unless it also
    implements :class:`ICliConfigWriter`.  Registered with
    ``builder.add_config_provider(...)``, through a module's
    ``config_providers``, or by a plugin's ``config_providers``
    (``mount_plugin``)."""

    #: Unique: lower-case letters, digits, ``_`` and ``-`` (``cors``, ``my-app``).
    id: str
    title: str
    description: Optional[str] = None
    #: Sort order of the providers (ascending, then by id); default 100.
    order: Optional[int] = None
    #: A permission needed, besides ``config:read`` / ``config:write``, to see or change this provider.
    required_permission: Optional[str] = None
    #: Where its settings come from.  Without it a writer is described as
    #: ``custom`` "Saved by the provider", a read-only provider as ``custom``
    #: "Read by the provider".
    storage: Optional[CliConfigProviderStorage] = None

    @abc.abstractmethod
    def get_sections(
        self, context: CliConfigContext,
    ) -> Union[Sequence[CliConfigSection], Awaitable[Sequence[CliConfigSection]]]:
        """The provider's sections (sync or async)."""


@dataclass
class CliConfigUpdateResult:
    """What a writer's ``update`` did."""

    #: The keys changed (or reset), in the order given.
    applied: list[str]
    #: True when one of them takes effect only after a restart.
    requires_restart: bool
    #: The section's new version.
    version: str


class ICliConfigWriter(abc.ABC):
    """The write side of a provider.  The server checks the permissions, that
    each key exists and is mutable, and its type and validation rules before
    calling ``validate``, then ``update``.  A ``None`` value resets the
    setting to its default (a secret: clears it)."""

    @abc.abstractmethod
    def validate(
        self, section_id: str, changes: dict[str, CliConfigValue], context: CliConfigContext,
    ) -> Union[CliConfigErrors, Awaitable[CliConfigErrors]]:
        """Further checks (e.g. between keys); per-key errors, empty when valid (sync or async)."""

    @abc.abstractmethod
    async def update(
        self,
        section_id: str,
        changes: dict[str, CliConfigValue],
        expected_version: Optional[str],
        context: CliConfigContext,
    ) -> CliConfigUpdateResult:
        """Applies the changes.  Raises :class:`CliConfigConflictError` when
        ``expected_version`` is given and is not the section's current version,
        :class:`CliConfigValidationError` for late errors."""


def is_cli_config_writer(provider: Any) -> bool:
    """True for a provider that can also write (:class:`ICliConfigWriter`, or duck-typed)."""
    return (
        provider is not None
        and callable(getattr(provider, "validate", None))
        and callable(getattr(provider, "update", None))
    )


async def maybe_await(value: Any) -> Any:
    """``await`` an awaitable, return anything else as it is."""
    if inspect.isawaitable(value):
        return await value
    return value


class CliConfigConflictError(Exception):
    """The section changed since ``expected_version`` (HTTP 409)."""

    def __init__(self, current_version: str) -> None:
        super().__init__("version conflict")
        self.current_version = current_version


class CliConfigValidationError(Exception):
    """Per-key validation errors found while updating (HTTP 400)."""

    def __init__(self, errors: CliConfigErrors) -> None:
        super().__init__("validation failed")
        self.errors = dict(errors)


class CliConfigPermissions:
    """The permissions of config providers."""

    #: See the providers, their sections and settings (secrets stay hidden).
    READ = "config:read"
    #: Change settings.
    WRITE = "config:write"


#: The event published after every change (audience ``config:read``).
CLI_CONFIG_CHANGED_EVENT = "config:changed"

#: The audit event written for every change.
CLI_CONFIG_AUDIT_EVENT = "config.update"

#: How a masked secret appears in audit entries.
CLI_CONFIG_SECRET_MASK = "***"

#: Provider ids: ``cors``, ``my-app``.
CLI_CONFIG_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")

#: Section ids and setting keys: letters, digits, ``_``, ``-`` and ``.`` (``allowedOrigins``, ``rate.limit``).
CLI_CONFIG_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")


class CliConfigError(Exception):
    """A refused read or change, as the API answers it: ``status`` is the
    HTTP status and the body is ``{error, errors?, version?}`` (``errors``:
    per-key messages on 400; ``version``: the current version on 409)."""

    def __init__(
        self,
        status: int,
        message: str,
        errors: Optional[CliConfigErrors] = None,
        version: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.errors = errors
        self.version = version

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.message}
        if self.errors:
            body["errors"] = dict(self.errors)
        if self.version is not None:
            body["version"] = self.version
        return body


class ICliConfigService(abc.ABC):
    """The server's configuration service (``builder.config``): the
    providers, filtered by permission, with secrets masked; changes
    validated, audited and published.  The admin plugin serves it over HTTP;
    the ``config`` command and the MCP tools use it too.  Views are the JSON
    the API returns (dicts with camelCase keys)."""

    @abc.abstractmethod
    async def list(self, context: CliConfigContext) -> list[dict[str, Any]]:
        """The providers the caller may see, sorted.  Raises 403 without ``config:read``."""

    def describe_default_store(self) -> CliConfigStoreDescription:
        """The store providers use unless they have their own (its own description)."""
        return CliConfigStoreDescription(kind="custom", title="Custom store", detail="", persistent=False)

    @abc.abstractmethod
    async def get(self, provider_id: str, context: CliConfigContext) -> dict[str, Any]:
        """One provider.  Raises :class:`CliConfigError` 403 / 404."""

    @abc.abstractmethod
    async def update(
        self, provider_id: str, section_id: str, request: Mapping[str, Any], context: CliConfigContext,
    ) -> dict[str, Any]:
        """Changes a section (``request``: ``{changes, version?}``).  Raises :class:`CliConfigError` 400 / 403 / 404 / 409."""


def cli_permission_matches(granted: str, required: str) -> bool:
    """``resource:action`` permission matching: a ``*`` as the last segment
    matches that level and everything below (``*``, ``config:*``); a ``*``
    elsewhere matches one segment.  Case-insensitive."""
    g = granted.strip().lower().split(":")
    r = required.strip().lower().split(":")
    for i, part in enumerate(g):
        if part == "*" and i == len(g) - 1:
            return True
        if i >= len(r) or (part != "*" and part != r[i]):
            return False
    return len(g) == len(r)


def has_cli_permission(granted: Optional[Sequence[str]], required: str) -> bool:
    """True when one of ``granted`` matches ``required``."""
    return any(isinstance(p, str) and cli_permission_matches(p, required) for p in granted or ())


def cli_config_context(
    caller: Optional[CliConfigCaller] = None,
    via: Optional[str] = None,
    has_permission: Optional[Callable[[str], bool]] = None,
) -> CliConfigContext:
    """The context of a caller with these permissions; without a caller (a
    server without authentication) every permission is granted.
    ``has_permission`` overrides the check (e.g. the auth context's ``can``)."""
    return CliConfigContext(caller=caller, via=via, permission_check=has_permission)
