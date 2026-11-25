"""The server's configuration service: permissions, validation, conflicts,
secrets masked, audit (``config.update``) and events (``config:changed``),
each stored provider with the store it saves to."""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Mapping, Optional

from qodalis_cli_server_abstractions.audit import AuditEntry
from qodalis_cli_server_abstractions.config import (
    CLI_CONFIG_AUDIT_EVENT,
    CLI_CONFIG_CHANGED_EVENT,
    CLI_CONFIG_KEY,
    CLI_CONFIG_SECRET_MASK,
    CliConfigConflictError,
    CliConfigContext,
    CliConfigEntry,
    CliConfigError,
    CliConfigErrors,
    CliConfigPermissions,
    CliConfigProviderStorage,
    CliConfigSection,
    CliConfigStoreDescription,
    CliConfigValidationError,
    ICliConfigService,
    clone_cli_config_value,
    describe_cli_config_store,
    is_cli_config_writer,
    maybe_await,
    validate_cli_config_value,
)
from qodalis_cli_server_abstractions.config.config_types import CliConfigValidation
from qodalis_cli_server_abstractions.events import CliEventAudience

from .cli_config_registry import CliConfigRegistry, _order

logger = logging.getLogger("qodalis_cli.config")

#: Where config changes are audited (``config.update``).
CliConfigAuditSink = Callable[[AuditEntry], Awaitable[None]]

_JSON_ENTRY = CliConfigEntry(key="", type="json")


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, list) and len(value) == 0)


def to_cli_config_entry_view(entry: Any, writable: bool) -> dict[str, Any]:
    """An entry as the API returns it: defaults filled, the value of a sensitive entry never included."""
    e = CliConfigEntry.of(entry)
    sensitive = e.sensitive or e.type == "secret"
    value = e.value
    view: dict[str, Any] = {
        "key": e.key,
        "label": e.label if e.label is not None else e.key,
        "description": e.description if e.description is not None else "",
        "type": e.type,
        "value": None if sensitive else clone_cli_config_value(value),
        "default": None if sensitive else clone_cli_config_value(e.default),
        "source": e.source or "default",
        "mutable": writable and e.mutable is True,
        "requiresRestart": e.requires_restart is True,
        "sensitive": sensitive,
    }
    if sensitive:
        view["isSet"] = bool(e.is_set) if e.is_set is not None else not _is_empty(value)
    validation = CliConfigValidation.of(e.validation)
    if validation is not None:
        view["validation"] = validation.to_dict()
    if e.editor:
        view["editor"] = e.editor
    return view


def to_cli_config_section_view(section: Any, writable: bool) -> dict[str, Any]:
    """A section as the API returns it; ``writable``: the provider implements the writer."""
    s = CliConfigSection.of(section)
    entries = [to_cli_config_entry_view(e, writable) for e in s.entries or []]
    allow_custom_keys = writable and s.allow_custom_keys is True
    mutable = allow_custom_keys or any(e["mutable"] for e in entries)
    return {
        "id": s.id,
        "title": s.title if s.title is not None else s.id,
        "description": s.description if s.description is not None else "",
        "mutable": mutable,
        "version": (s.version if s.version is not None else None) if mutable else None,
        "allowCustomKeys": allow_custom_keys,
        "entries": entries,
    }


def cli_config_provider_storage_of(provider: Any) -> dict[str, Any]:
    """A provider's ``storage`` as the API returns it: its own, else ``custom`` (saved / read by the provider)."""
    own = getattr(provider, "storage", None)
    if isinstance(own, Mapping):
        own = CliConfigProviderStorage(
            kind=own.get("kind"), description=own.get("description") or "", sources=own.get("sources") or [],
        ) if isinstance(own.get("kind"), str) else None
    if isinstance(own, CliConfigProviderStorage) and isinstance(own.kind, str):
        sources = own.sources if isinstance(own.sources, (list, tuple)) else []
        return {
            "kind": own.kind,
            "description": own.description or "",
            "sources": [s for s in sources if isinstance(s, str)],
        }
    return {
        "kind": "custom",
        "description": "Saved by the provider" if is_cli_config_writer(provider) else "Read by the provider",
        "sources": [],
    }


def _audit_value(view: Optional[dict[str, Any]], raw: Any, side: str) -> Any:
    """How a value appears in the audit log: a secret as ``***``, or ``None`` when cleared/unset."""
    if view is not None and view.get("sensitive"):
        if side == "old":
            return CLI_CONFIG_SECRET_MASK if view.get("isSet") else None
        return None if raw is None or raw == "" else CLI_CONFIG_SECRET_MASK
    if side == "old":
        current = view.get("value") if view is not None else None
    else:
        current = view.get("value") if view is not None and view.get("value") is not None else raw
    return clone_cli_config_value(current)


class CliConfigService(ICliConfigService):
    """The server's config providers, read and changed with permissions,
    validation, conflicts, secrets masked, audit (``config.update``) and
    events (``config:changed``), each stored provider with the store it
    saves to: the HTTP API (admin plugin), the ``config`` command and the MCP
    tools all go through it."""

    def __init__(
        self,
        registry: CliConfigRegistry,
        audit: Optional[Callable[[], Optional[CliConfigAuditSink]]] = None,
        event_bus: Optional[Callable[[], Any]] = None,
        restart: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.registry = registry
        self._audit = audit
        self._event_bus = event_bus
        #: The restart service: a change to a ``requiresRestart`` entry is marked pending
        #: (``config:<provider>.<section>.<key>``).
        self._restart = restart
        #: The value of each ``requiresRestart`` entry changed so far, as it was at start (by pending id).
        self._start_values: dict[str, Any] = {}

    def attach(
        self,
        audit: Optional[Callable[[], Optional[CliConfigAuditSink]]] = None,
        event_bus: Optional[Callable[[], Any]] = None,
    ) -> None:
        """Sets where changes are audited and published (``create_cli_server`` does)."""
        if audit is not None:
            self._audit = audit
        if event_bus is not None:
            self._event_bus = event_bus

    async def ready(self) -> None:
        """Resolves once every provider has applied its stored settings."""
        await self.registry.ready()

    @staticmethod
    def _require_permission(context: CliConfigContext, permission: str) -> None:
        if not context.has_permission(permission):
            raise CliConfigError(403, f"forbidden: missing permission {permission}")

    @staticmethod
    def _can_see(provider: Any, context: CliConfigContext) -> bool:
        required = getattr(provider, "required_permission", None)
        return not required or context.has_permission(required)

    def _provider(self, provider_id: Any, context: CliConfigContext) -> Any:
        provider = self.registry.find(provider_id) if isinstance(provider_id, str) else None
        if provider is None:
            raise CliConfigError(404, f"unknown config provider: {provider_id}")
        if not self._can_see(provider, context):
            raise CliConfigError(403, f"forbidden: missing permission {provider.required_permission}")
        return provider

    @staticmethod
    async def _sections(provider: Any, context: CliConfigContext) -> list[CliConfigSection]:
        sections = await maybe_await(provider.get_sections(context))
        return [CliConfigSection.of(s) for s in sections or []]

    async def _view(self, provider: Any, context: CliConfigContext) -> dict[str, Any]:
        writer = is_cli_config_writer(provider)
        sections = await self._sections(provider, context)
        view: dict[str, Any] = {
            "id": provider.id,
            "title": provider.title,
            "description": getattr(provider, "description", None) or "",
            "order": _order(provider),
            "writable": writer and context.has_permission(CliConfigPermissions.WRITE),
            "storage": cli_config_provider_storage_of(provider),
        }
        store = self.describe_provider_store(provider)
        if store is not None:
            view["store"] = store.to_dict()
        view["sections"] = [to_cli_config_section_view(s, writer) for s in sections]
        return view

    def describe_default_store(self) -> CliConfigStoreDescription:
        """The store providers use unless they have their own: its own description (``custom`` when it has none)."""
        return describe_cli_config_store(self.registry.default_store)

    def describe_provider_store(self, provider: Any) -> Optional[CliConfigStoreDescription]:
        """The store a provider's changes are saved to; ``None`` for a provider without one (read-only, or saving itself)."""
        store = self.registry.store_of(provider)
        return describe_cli_config_store(store) if store is not None else None

    async def list(self, context: CliConfigContext) -> list[dict[str, Any]]:
        self._require_permission(context, CliConfigPermissions.READ)
        await self.ready()
        views: list[dict[str, Any]] = []
        for provider in self.registry.providers:
            if not self._can_see(provider, context):
                continue
            try:
                views.append(await self._view(provider, context))
            except Exception as exc:  # one failing provider does not hide the others
                logger.error("Config provider '%s' failed: %s", provider.id, exc)
        return views

    async def get(self, provider_id: str, context: CliConfigContext) -> dict[str, Any]:
        self._require_permission(context, CliConfigPermissions.READ)
        await self.ready()
        return await self._view(self._provider(provider_id, context), context)

    async def update(
        self,
        provider_id: str,
        section_id: str,
        request: Mapping[str, Any],
        context: CliConfigContext,
    ) -> dict[str, Any]:
        self._require_permission(context, CliConfigPermissions.WRITE)
        await self.ready()
        provider = self._provider(provider_id, context)
        if not is_cli_config_writer(provider):
            raise CliConfigError(400, f"config provider {provider.id} is read-only")
        changes = request.get("changes") if isinstance(request, Mapping) else None
        if not isinstance(changes, dict) or len(changes) == 0:
            raise CliConfigError(400, "changes must be a non-empty object")
        expected = request.get("version")
        if expected is not None and not isinstance(expected, str):
            raise CliConfigError(400, "version must be a string")
        section = next((s for s in await self._sections(provider, context) if s.id == section_id), None)
        if section is None:
            raise CliConfigError(404, f"unknown config section: {provider.id}.{section_id}")
        before = to_cli_config_section_view(section, True)
        if not before["mutable"]:
            raise CliConfigError(400, f"config section {provider.id}.{section_id} is read-only")

        # The server's checks: the key exists and is mutable, the value has the type and passes the rules
        errors: CliConfigErrors = {}
        for key, value in changes.items():
            entry = next((e for e in section.entries if e.key == key), None)
            if entry is None:
                if not before["allowCustomKeys"]:
                    errors[key] = "unknown setting"
                elif not isinstance(key, str) or not CLI_CONFIG_KEY.match(key):
                    errors[key] = "invalid key: use letters, digits, _, - and ."
                elif value is not None:
                    error = validate_cli_config_value(_JSON_ENTRY, value)
                    if error:
                        errors[key] = error
                continue
            if entry.mutable is not True:
                errors[key] = "read-only"
                continue
            error = validate_cli_config_value(entry, value)
            if error:
                errors[key] = error
        if not errors:
            errors.update(await maybe_await(provider.validate(section_id, dict(changes), context)) or {})
        if errors:
            raise CliConfigError(400, "validation failed", errors)
        if isinstance(expected, str) and before["version"] is not None and expected != before["version"]:
            raise CliConfigError(409, "version conflict", None, before["version"])

        try:
            result = await provider.update(section_id, dict(changes), expected, context)
        except CliConfigConflictError as exc:
            raise CliConfigError(409, "version conflict", None, exc.current_version) from exc
        except CliConfigValidationError as exc:
            raise CliConfigError(400, "validation failed", exc.errors) from exc

        updated = next((s for s in await self._sections(provider, context) if s.id == section_id), None)
        after = to_cli_config_section_view(updated or section, True)
        audit_changes = []
        for key in result.applied:
            old = next((e for e in before["entries"] if e["key"] == key), None)
            now = next((e for e in after["entries"] if e["key"] == key), None)
            raw = changes.get(key)
            new = _audit_value(now or old, raw, "new") if (now is not None or raw is not None) else None
            audit_changes.append({"key": key, "old": _audit_value(old, None, "old"), "new": new})
        self._track_restart(provider, section_id, result.applied, before, after)
        store = self.describe_provider_store(provider)
        store_kind = store.kind if store is not None else None
        await self._write_audit(provider.id, section_id, result.version, audit_changes, context, store_kind)
        await self._publish(provider.id, section_id, result, context, store_kind)
        if after["mutable"]:
            after["version"] = result.version
        return {
            "provider": provider.id,
            "section": after,
            "applied": list(result.applied),
            "requiresRestart": result.requires_restart,
            "version": result.version,
        }

    def _track_restart(
        self, provider: Any, section_id: str, applied: Any, before: dict[str, Any], after: dict[str, Any],
    ) -> None:
        """Marks a changed ``requiresRestart`` entry as waiting for a restart, and clears it when its value is set
        back to what it was at start (a secret, whose value is never seen, stays marked)."""
        restart = self._restart() if self._restart is not None else None
        if restart is None:
            return
        for key in applied:
            old = next((e for e in before["entries"] if e["key"] == key), None)
            now = next((e for e in after["entries"] if e["key"] == key), None)
            if not (old or {}).get("requiresRestart") and not (now or {}).get("requiresRestart"):
                continue
            pending_id = f"config:{provider.id}.{section_id}.{key}"
            sensitive = bool((old or {}).get("sensitive") or (now or {}).get("sensitive"))
            if pending_id not in self._start_values:
                self._start_values[pending_id] = clone_cli_config_value((old or {}).get("value"))
            unchanged = not sensitive and (now or {}).get("value") == self._start_values[pending_id]
            label = (now or old or {}).get("label") or key
            try:
                if unchanged:
                    restart.clear_required(pending_id)
                else:
                    restart.mark_required(pending_id, "config", f"{provider.title}: {label} changed")
            except Exception as exc:  # noqa: BLE001 - the change is made; marking failing is logged
                logger.error("Marking a restart failed: %s", exc)

    async def _publish(
        self,
        provider_id: str,
        section_id: str,
        result: Any,
        context: CliConfigContext,
        store_kind: Optional[str] = None,
    ) -> None:
        bus = self._event_bus() if self._event_bus is not None else None
        if bus is None:
            return
        data: dict[str, Any] = {
            "provider": provider_id,
            "section": section_id,
            "keys": list(result.applied),
            "version": result.version,
            "requiresRestart": result.requires_restart,
        }
        if store_kind:
            data["store"] = store_kind
        user_name = context.caller.user_name if context.caller is not None else None
        if user_name:
            data["user"] = user_name
        try:
            await maybe_await(bus.publish(
                CLI_CONFIG_CHANGED_EVENT, data, CliEventAudience(permission=CliConfigPermissions.READ),
            ))
        except Exception as exc:  # the change is made: never fail it for the event
            logger.error("Publishing a config change failed: %s", exc)

    async def _write_audit(
        self,
        provider_id: str,
        section_id: str,
        version: str,
        changes: list[dict[str, Any]],
        context: CliConfigContext,
        store_kind: Optional[str] = None,
    ) -> None:
        sink = self._audit() if self._audit is not None else None
        if sink is None:
            return
        caller = context.caller
        user_name = (caller.user_name if caller is not None else None) or "anonymous"
        user_id = caller.user_id if caller is not None else None
        properties: dict[str, Any] = {"provider": provider_id, "section": section_id, "version": version}
        if store_kind:
            properties["store"] = store_kind
        if context.via:
            properties["via"] = context.via
        properties["changes"] = changes
        try:
            await sink(AuditEntry(
                timestamp=int(time.time() * 1000),
                event=CLI_CONFIG_AUDIT_EVENT,
                username=user_name,
                user_id=user_id or None,
                actor_id=user_id or None,
                actor=user_name,
                target=f"config:{provider_id}.{section_id}",
                details=f"{provider_id}.{section_id}: {', '.join(c['key'] for c in changes)}",
                properties=properties,
            ))
        except Exception as exc:
            logger.error("Auditing a config change failed: %s", exc)
