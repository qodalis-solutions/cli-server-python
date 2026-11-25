"""Config registry and service: registration, ordering, the store, permission
filtering, validation, conflicts, secret masking, audit (``config.update``)
and the ``config:changed`` event.  Port of the Node server's
``config-service.test.ts``."""

from __future__ import annotations

import datetime
import json
from typing import Any, Optional

import pytest

from qodalis_cli.config import CliConfigRegistry, CliConfigService, to_cli_config_entry_view, to_cli_config_section_view
from qodalis_cli.services.cli_event_bus import CliEventBus
from qodalis_cli_server_abstractions.audit import AuditEntry
from qodalis_cli_server_abstractions.config import (
    CliConfigCaller,
    CliConfigConflictError,
    CliConfigContext,
    CliConfigError,
    CliConfigSectionDefinition,
    CliConfigSettingDefinition,
    CliConfigUpdateResult,
    CliConfigValidation,
    CliConfigValidationError,
    CliStoredConfigProvider,
    InMemoryConfigStore,
    cli_config_context,
    create_custom_config_provider,
)
from qodalis_cli_server_abstractions.config.config_store import CliConfigStoredSection
from qodalis_cli_server_abstractions.events import CliEventAudience

ALL = cli_config_context(None, "api")


def as_(*permissions: str) -> CliConfigContext:
    return cli_config_context(CliConfigCaller(user_id="u1", user_name="ann", permissions=tuple(permissions)), "api")


WRITE = as_("config:read", "config:write")


class ReadOnly:
    """A read-only provider (no writer)."""

    def __init__(self, id: str, order: Optional[int] = None, required_permission: Optional[str] = None) -> None:
        self.id = id
        self.title = id.upper()
        self.order = order
        self.required_permission = required_permission

    def get_sections(self, context: Any = None) -> list[dict[str, Any]]:
        return [{"id": "main", "entries": [{"key": "k", "type": "string", "value": self.id}]}]


def app() -> CliStoredConfigProvider:
    return CliStoredConfigProvider(
        id="app",
        title="App",
        sections=[
            CliConfigSectionDefinition(id="main", settings=[
                CliConfigSettingDefinition(
                    key="name", type="string", default="demo", validation=CliConfigValidation(required=True, max=10),
                ),
                CliConfigSettingDefinition(
                    key="limit", type="number", default=5, validation=CliConfigValidation(min=1, max=10),
                ),
                CliConfigSettingDefinition(key="apiKey", type="secret"),
                CliConfigSettingDefinition(
                    key="mode", type="enum", default="a", validation=CliConfigValidation(options=["a", "b"]),
                ),
                CliConfigSettingDefinition(key="fixed", type="string", mutable=False, default="x"),
            ]),
            CliConfigSectionDefinition(id="info", settings=[
                CliConfigSettingDefinition(key="build", type="string", mutable=False, default="1"),
            ]),
        ],
    )


class Setup:
    def __init__(self, providers: Optional[list[Any]] = None) -> None:
        self.registry = CliConfigRegistry()
        for p in providers if providers is not None else [app()]:
            self.registry.add(p)
        self.registry.start()
        self.audits: list[AuditEntry] = []
        self.events: list[Any] = []
        bus = CliEventBus()
        bus.subscribe("config:*", lambda e: self.events.append(e))

        async def sink(entry: AuditEntry) -> None:
            self.audits.append(entry)

        self.service = CliConfigService(self.registry, audit=lambda: sink, event_bus=lambda: bus)


async def refused(awaitable: Any) -> CliConfigError:
    with pytest.raises(CliConfigError) as info:
        await awaitable
    return info.value


def entry(view: dict[str, Any], key: str, section: int = 0) -> dict[str, Any]:
    return next(e for e in view["sections"][section]["entries"] if e["key"] == key)


# ---------------------------------------------------------------------------
# CliConfigRegistry
# ---------------------------------------------------------------------------


class _NoSections:
    id = "x"
    title = "X"


class _NoTitle:
    id = "x"
    title = ""

    def get_sections(self, context: Any = None) -> list:
        return []


class TestRegistry:
    def test_validates_ids_and_titles_refuses_duplicates(self) -> None:
        r = CliConfigRegistry()
        with pytest.raises(ValueError, match="invalid config provider: Bad Id"):
            r.add(ReadOnly("Bad Id"))
        with pytest.raises(ValueError, match="needs a title"):
            r.add(_NoTitle())
        with pytest.raises(ValueError, match="invalid config provider: x"):
            r.add(_NoSections())
        r.add(ReadOnly("a"))
        with pytest.raises(ValueError, match="config provider already registered: a"):
            r.add(ReadOnly("a"))
        assert r.has("a") is True
        assert r.find("a").title == "A"
        assert r.find("b") is None

    def test_sorts_by_order_default_100_then_id(self) -> None:
        r = CliConfigRegistry()
        r.add(ReadOnly("zeta"))
        r.add(ReadOnly("alpha"))
        r.add(ReadOnly("first", 0))
        r.add(ReadOnly("last", 1000))
        assert [p.id for p in r.providers] == ["first", "alpha", "zeta", "last"]

    async def test_store_in_memory_by_default_set_before_start_attached_also_later(self) -> None:
        r = CliConfigRegistry()
        assert r.store.source == "runtime"
        store = InMemoryConfigStore()
        await store.write("app", "main", {"limit": 9})
        await store.write("custom", "custom", {"a": 1})
        r.set_store(store)
        early = app()
        r.add(early)
        assert r.started is False
        r.start()
        r.start()
        with pytest.raises(RuntimeError, match="the config store must be set before the server is created"):
            r.set_store(InMemoryConfigStore())
        late = create_custom_config_provider()
        r.add(late)
        await r.ready()
        assert early.value("main", "limit") == 9
        assert late.value("custom", "a") == 1

    async def test_ready_raises_when_a_provider_fails_to_load(self) -> None:
        class Down(InMemoryConfigStore):
            source = "database"

            async def read(self, provider: str, section: str) -> CliConfigStoredSection:
                raise RuntimeError("down")

            async def write(self, *args: Any, **kwargs: Any) -> CliConfigStoredSection:
                raise RuntimeError("down")

        r = CliConfigRegistry()
        r.set_store(Down())
        r.add(app())
        r.start()
        with pytest.raises(RuntimeError, match="down"):
            await r.ready()


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------


class TestViews:
    def test_entry_defaults_filled_sensitive_masked_is_set(self) -> None:
        assert to_cli_config_entry_view({"key": "k", "type": "string", "value": "v"}, True) == {
            "key": "k",
            "label": "k",
            "description": "",
            "type": "string",
            "value": "v",
            "default": None,
            "source": "default",
            "mutable": False,
            "requiresRestart": False,
            "sensitive": False,
        }
        view = to_cli_config_entry_view(
            {"key": "p", "type": "secret", "value": "hunter2", "default": "x", "mutable": True}, True,
        )
        assert view["value"] is None and view["default"] is None
        assert view["sensitive"] is True and view["isSet"] is True and view["mutable"] is True
        url = to_cli_config_entry_view({"key": "p", "type": "url", "value": "https://u:p@h", "sensitive": True}, True)
        assert url["value"] is None and url["isSet"] is True
        assert to_cli_config_entry_view({"key": "p", "type": "secret", "isSet": False}, True)["isSet"] is False
        assert to_cli_config_entry_view({"key": "p", "type": "secret", "value": ""}, True)["isSet"] is False
        # Not a writer: nothing is mutable
        assert to_cli_config_entry_view({"key": "k", "type": "string", "mutable": True}, False)["mutable"] is False
        tags = to_cli_config_entry_view(
            {"key": "k", "type": "string", "editor": "tags", "validation": {"options": ["a"]}}, True,
        )
        assert tags["editor"] == "tags"
        assert tags["validation"] == {"options": ["a"]}

    def test_section_mutable_version_only_when_mutable_allow_custom_keys_only_for_writers(self) -> None:
        section = {"id": "s", "entries": [{"key": "k", "type": "string", "mutable": True}], "version": "3"}
        view = to_cli_config_section_view(section, True)
        assert {k: view[k] for k in ("id", "title", "description", "mutable", "version", "allowCustomKeys")} == {
            "id": "s", "title": "s", "description": "", "mutable": True, "version": "3", "allowCustomKeys": False,
        }
        ro = to_cli_config_section_view(section, False)
        assert ro["mutable"] is False and ro["version"] is None
        custom = to_cli_config_section_view({"id": "s", "entries": [], "allowCustomKeys": True, "version": "0"}, True)
        assert custom["mutable"] is True and custom["allowCustomKeys"] is True
        custom_ro = to_cli_config_section_view({"id": "s", "entries": [], "allowCustomKeys": True}, False)
        assert custom_ro["mutable"] is False and custom_ro["allowCustomKeys"] is False
        no_version = to_cli_config_section_view({"id": "s", "entries": [{"key": "k", "type": "string", "mutable": True}]}, True)
        assert no_version["version"] is None


# ---------------------------------------------------------------------------
# CliConfigService: reading
# ---------------------------------------------------------------------------


class TestReading:
    async def test_list_needs_config_read(self) -> None:
        s = Setup()
        err = await refused(s.service.list(as_()))
        assert err.status == 403
        assert err.message == "forbidden: missing permission config:read"
        assert [p["id"] for p in await s.service.list(as_("config:read"))] == ["app"]

    async def test_list_filters_by_required_permission_and_sorts(self) -> None:
        s = Setup([ReadOnly("b"), ReadOnly("a", 1), ReadOnly("secret-stuff", 0, "ops:root")])
        assert [p["id"] for p in await s.service.list(as_("config:read"))] == ["a", "b"]
        assert [p["id"] for p in await s.service.list(as_("config:read", "ops:root"))] == ["secret-stuff", "a", "b"]
        assert [p["id"] for p in await s.service.list(ALL)] == ["secret-stuff", "a", "b"]

    async def test_provider_view_writable_only_for_a_writer_and_config_write(self) -> None:
        s = Setup([app(), ReadOnly("ro")])
        app_view, ro = await s.service.list(as_("config:read"))
        assert {k: app_view[k] for k in ("id", "title", "description", "order", "writable")} == {
            "id": "app", "title": "App", "description": "", "order": 100, "writable": False,
        }
        assert (await s.service.get("app", as_("config:read", "config:write")))["writable"] is True
        assert ro["writable"] is False
        assert ro["sections"][0]["mutable"] is False and ro["sections"][0]["version"] is None
        # Entries stay mutable (what the provider allows); writable says what the caller may do
        assert entry(app_view, "limit")["mutable"] is True

    async def test_a_failing_provider_is_left_out_of_the_list(self) -> None:
        class Broken:
            id = "broken"
            title = "Broken"

            async def get_sections(self, context: Any = None) -> list:
                raise RuntimeError("boom")

        s = Setup([Broken(), ReadOnly("ok")])
        assert [p["id"] for p in await s.service.list(ALL)] == ["ok"]
        with pytest.raises(RuntimeError, match="boom"):
            await s.service.get("broken", ALL)

    async def test_get_403_404_and_required_permission(self) -> None:
        s = Setup([ReadOnly("a"), ReadOnly("guarded", 1, "ops:root")])
        assert (await refused(s.service.get("a", as_()))).status == 403
        err = await refused(s.service.get("nope", as_("config:read")))
        assert (err.status, err.message) == (404, "unknown config provider: nope")
        err = await refused(s.service.get("guarded", as_("config:read")))
        assert (err.status, err.message) == (403, "forbidden: missing permission ops:root")
        assert (await s.service.get("guarded", as_("config:read", "ops:root")))["id"] == "guarded"

    async def test_never_returns_a_secret(self) -> None:
        s = Setup()
        await s.service.update("app", "main", {"changes": {"apiKey": "sk_live_42"}}, ALL)
        assert "sk_live_42" not in json.dumps(await s.service.list(ALL))
        view = await s.service.get("app", ALL)
        api_key = entry(view, "apiKey")
        assert api_key["value"] is None and api_key["isSet"] is True and api_key["sensitive"] is True

    async def test_a_provider_leaking_a_secret_value_is_masked_anyway(self) -> None:
        class Leaky:
            id = "leaky"
            title = "Leaky"

            def get_sections(self, context: Any = None) -> list:
                return [{"id": "s", "entries": [{"key": "token", "type": "secret", "value": "oops"}]}]

        s = Setup([Leaky()])
        assert "oops" not in json.dumps(await s.service.get("leaky", ALL))


# ---------------------------------------------------------------------------
# CliConfigService: update
# ---------------------------------------------------------------------------


class Writer:
    """A provider with its own writer; ``update`` answers from ``outcomes`` in turn."""

    def __init__(self, entries: list[dict], validate: Any = None, outcomes: Optional[list[Any]] = None) -> None:
        self.id = "w"
        self.title = "W"
        self._entries = entries
        self._validate = validate
        self.outcomes = list(outcomes or [])
        self.calls = 0

    def get_sections(self, context: Any = None) -> list:
        return [{"id": "s", "version": "1", "entries": self._entries}]

    def validate(self, section_id: str, changes: dict, context: Any = None) -> dict:
        return self._validate(section_id, changes) if self._validate else {}

    async def update(self, section_id: str, changes: dict, expected: Any, context: Any = None) -> CliConfigUpdateResult:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class TestUpdate:
    async def test_needs_config_write(self) -> None:
        s = Setup()
        err = await refused(s.service.update("app", "main", {"changes": {"limit": 2}}, as_("config:read")))
        assert (err.status, err.message) == (403, "forbidden: missing permission config:write")

    async def test_404_unknown_provider_or_section_403_guarded(self) -> None:
        guarded = CliStoredConfigProvider(
            id="g", title="G", required_permission="ops:root",
            sections=[CliConfigSectionDefinition(id="s", settings=[CliConfigSettingDefinition(key="k", type="string")])],
        )
        s = Setup([app(), guarded])
        assert (await refused(s.service.update("nope", "main", {"changes": {"a": 1}}, WRITE))).status == 404
        err = await refused(s.service.update("app", "nope", {"changes": {"a": 1}}, WRITE))
        assert (err.status, err.message) == (404, "unknown config section: app.nope")
        assert (await refused(s.service.update("g", "s", {"changes": {"k": "v"}}, WRITE))).status == 403
        result = await s.service.update("g", "s", {"changes": {"k": "v"}}, as_("config:write", "config:read", "ops:root"))
        assert result["version"] == "1"

    async def test_400_for_a_read_only_provider_or_section(self) -> None:
        s = Setup([app(), ReadOnly("ro")])
        err = await refused(s.service.update("ro", "main", {"changes": {"k": "x"}}, WRITE))
        assert (err.status, err.message) == (400, "config provider ro is read-only")
        err = await refused(s.service.update("app", "info", {"changes": {"build": "2"}}, WRITE))
        assert (err.status, err.message) == (400, "config section app.info is read-only")

    async def test_400_for_a_malformed_body(self) -> None:
        s = Setup()
        for changes in (None, [], "x", {}):
            err = await refused(s.service.update("app", "main", {"changes": changes}, WRITE))
            assert (err.status, err.message) == (400, "changes must be a non-empty object")
        err = await refused(s.service.update("app", "main", {}, WRITE))
        assert (err.status, err.message) == (400, "changes must be a non-empty object")
        err = await refused(s.service.update("app", "main", {"changes": {"limit": 2}, "version": 3}, WRITE))
        assert (err.status, err.message) == (400, "version must be a string")

    async def test_400_with_per_key_errors(self) -> None:
        s = Setup()
        err = await refused(s.service.update(
            "app", "main",
            {"changes": {"nope": 1, "fixed": "y", "limit": "ten", "name": "", "mode": "c", "apiKey": 5}},
            WRITE,
        ))
        assert err.status == 400
        assert err.message == "validation failed"
        assert err.errors == {
            "nope": "unknown setting",
            "fixed": "read-only",
            "limit": "must be a number",
            "name": "is required",
            "mode": "must be one of: a, b",
            "apiKey": "must be a string",
        }
        err = await refused(s.service.update("app", "main", {"changes": {"limit": 11, "name": "much too long"}}, WRITE))
        assert err.errors == {"limit": "must be at most 10", "name": "must be at most 10 characters"}
        # Nothing changed
        view = await s.service.get("app", ALL)
        assert view["sections"][0]["version"] == "0"

    async def test_the_writers_own_validate_errors_are_returned_too(self) -> None:
        writer = Writer(
            [{"key": "a", "type": "number", "mutable": True}, {"key": "b", "type": "number", "mutable": True}],
            validate=lambda _s, c: {"a": "must not exceed b"} if c["a"] > c["b"] else {},
            outcomes=[CliConfigUpdateResult(applied=["a", "b"], requires_restart=False, version="2")],
        )
        s = Setup([writer])
        err = await refused(s.service.update("w", "s", {"changes": {"a": 5, "b": 1}}, WRITE))
        assert err.status == 400 and err.errors == {"a": "must not exceed b"}
        assert writer.calls == 0
        assert (await s.service.update("w", "s", {"changes": {"a": 1, "b": 5}}, WRITE))["version"] == "2"

    async def test_409_for_a_stale_version_nothing_changes(self) -> None:
        s = Setup()
        await s.service.update("app", "main", {"changes": {"limit": 2}, "version": "0"}, WRITE)
        err = await refused(s.service.update("app", "main", {"changes": {"limit": 3}, "version": "0"}, WRITE))
        assert (err.status, err.message, err.version) == (409, "version conflict", "1")
        assert err.to_dict() == {"error": "version conflict", "version": "1"}
        assert len(s.audits) == 1
        assert len(s.events) == 1
        assert entry(await s.service.get("app", ALL), "limit")["value"] == 2

    async def test_409_when_the_writer_reports_a_conflict_400_for_late_errors(self) -> None:
        writer = Writer(
            [{"key": "a", "type": "number", "mutable": True}],
            outcomes=[CliConfigConflictError("7"), CliConfigValidationError({"a": "taken"}), RuntimeError("disk full")],
        )
        s = Setup([writer])
        err = await refused(s.service.update("w", "s", {"changes": {"a": 1}, "version": "1"}, WRITE))
        assert (err.status, err.version) == (409, "7")
        err = await refused(s.service.update("w", "s", {"changes": {"a": 1}}, WRITE))
        assert (err.status, err.errors) == (400, {"a": "taken"})
        with pytest.raises(RuntimeError, match="disk full"):
            await s.service.update("w", "s", {"changes": {"a": 1}}, WRITE)

    async def test_applies_without_a_version(self) -> None:
        s = Setup()
        result = await s.service.update("app", "main", {"changes": {"limit": 7, "mode": "b"}}, WRITE)
        assert result["provider"] == "app"
        assert result["applied"] == ["limit", "mode"]
        assert result["requiresRestart"] is False
        assert result["version"] == "1"
        assert result["section"]["version"] == "1"
        limit = next(e for e in result["section"]["entries"] if e["key"] == "limit")
        assert (limit["value"], limit["source"]) == (7, "runtime")

    async def test_null_resets_to_the_default(self) -> None:
        s = Setup()
        await s.service.update("app", "main", {"changes": {"limit": 7}}, WRITE)
        result = await s.service.update("app", "main", {"changes": {"limit": None}}, WRITE)
        limit = next(e for e in result["section"]["entries"] if e["key"] == "limit")
        assert (limit["value"], limit["source"]) == (5, "default")

    async def test_custom_keys_added_typed_by_value_removed_with_null(self) -> None:
        s = Setup([create_custom_config_provider()])
        added = await s.service.update("custom", "custom", {"changes": {"motd": "hi", "feature.x": True}}, WRITE)
        assert [(e["key"], e["type"], e["value"]) for e in added["section"]["entries"]] == [
            ("motd", "string", "hi"),
            ("feature.x", "boolean", True),
        ]
        err = await refused(s.service.update("custom", "custom", {"changes": {"bad key": 1}}, WRITE))
        assert err.status == 400
        assert err.errors == {"bad key": "invalid key: use letters, digits, _, - and ."}
        # Not JSON (Node: undefined and a Date; Python: a datetime)
        err = await refused(s.service.update(
            "custom", "custom", {"changes": {"fn": {"d": datetime.datetime.now()}}}, WRITE,
        ))
        assert err.status == 400
        removed = await s.service.update("custom", "custom", {"changes": {"motd": None}}, WRITE)
        assert [e["key"] for e in removed["section"]["entries"]] == ["feature.x"]


# ---------------------------------------------------------------------------
# CliConfigService: audit and event
# ---------------------------------------------------------------------------


class TestAuditAndEvent:
    async def test_audits_config_update_with_old_and_new_values(self) -> None:
        s = Setup()
        await s.service.update("app", "main", {"changes": {"limit": 8, "mode": "b"}, "version": "0"}, WRITE)
        assert len(s.audits) == 1
        e = s.audits[0]
        assert e.event == "config.update"
        assert e.username == "ann"
        assert e.user_id == "u1"
        assert e.actor == "ann"
        assert e.actor_id == "u1"
        assert e.target == "config:app.main"
        assert e.details == "app.main: limit, mode"
        assert e.properties == {
            "provider": "app",
            "section": "main",
            "version": "1",
            "via": "api",
            "changes": [
                {"key": "limit", "old": 5, "new": 8},
                {"key": "mode", "old": "a", "new": "b"},
            ],
        }
        assert isinstance(e.timestamp, int)

    async def test_secrets_are_masked_in_the_audit(self) -> None:
        s = Setup()
        await s.service.update("app", "main", {"changes": {"apiKey": "first-secret"}}, WRITE)
        await s.service.update("app", "main", {"changes": {"apiKey": "second-secret"}}, WRITE)
        await s.service.update("app", "main", {"changes": {"apiKey": None}}, WRITE)
        assert [a.properties["changes"][0] for a in s.audits] == [
            {"key": "apiKey", "old": None, "new": "***"},
            {"key": "apiKey", "old": "***", "new": "***"},
            {"key": "apiKey", "old": "***", "new": None},
        ]
        dumped = json.dumps([a.to_dict() for a in s.audits])
        assert "first-secret" not in dumped and "second-secret" not in dumped

    async def test_anonymous_callers_and_removed_custom_keys(self) -> None:
        s = Setup([create_custom_config_provider()])
        await s.service.update("custom", "custom", {"changes": {"a": 1}}, ALL)
        await s.service.update("custom", "custom", {"changes": {"a": None}}, ALL)
        assert s.audits[0].username == "anonymous" and s.audits[0].actor == "anonymous"
        assert s.audits[0].user_id is None
        assert s.audits[0].properties["changes"] == [{"key": "a", "old": None, "new": 1}]
        assert s.audits[1].properties["changes"] == [{"key": "a", "old": 1, "new": None}]

    async def test_a_failing_audit_sink_does_not_fail_the_change_no_sink_no_audit(self) -> None:
        registry = CliConfigRegistry()
        registry.add(app())

        async def down(_: AuditEntry) -> None:
            raise RuntimeError("audit down")

        service = CliConfigService(registry, audit=lambda: down)
        assert (await service.update("app", "main", {"changes": {"limit": 2}}, ALL))["version"] == "1"
        bare = CliConfigService(registry)
        assert (await bare.update("app", "main", {"changes": {"limit": 3}}, ALL))["version"] == "2"

    async def test_attach_sets_the_sink_and_the_bus(self) -> None:
        registry = CliConfigRegistry()
        registry.add(app())
        audits: list[AuditEntry] = []
        events: list[Any] = []
        bus = CliEventBus()
        bus.subscribe("config:*", lambda e: events.append(e))

        async def sink(e: AuditEntry) -> None:
            audits.append(e)

        service = CliConfigService(registry)
        service.attach(audit=lambda: sink, event_bus=lambda: bus)
        await service.update("app", "main", {"changes": {"limit": 2}}, ALL)
        assert len(audits) == 1 and len(events) == 1

    async def test_publishes_config_changed_for_config_read_holders_without_values(self) -> None:
        s = Setup()
        await s.service.update("app", "main", {"changes": {"apiKey": "shh", "limit": 2}}, WRITE)
        assert len(s.events) == 1
        event = s.events[0]
        assert event.type == "config:changed"
        assert event.audience == CliEventAudience(permission="config:read")
        assert event.data == {
            "provider": "app", "section": "main", "keys": ["apiKey", "limit"],
            "version": "1", "requiresRestart": False, "user": "ann",
        }
        assert "shh" not in json.dumps(event.data)

    async def test_no_audit_and_no_event_for_refused_changes(self) -> None:
        s = Setup()
        with pytest.raises(CliConfigError):
            await s.service.update("app", "main", {"changes": {"limit": 99}}, WRITE)
        with pytest.raises(CliConfigError):
            await s.service.update("app", "main", {"changes": {"limit": 2}}, as_("config:read"))
        assert s.audits == []
        assert s.events == []
