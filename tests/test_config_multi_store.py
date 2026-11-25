"""A config store per provider: the resolution order (the host's store for
the id, the provider's own, the default store, memory), providers in
different stores and one store shared by two, versions and conflicts per
store, ready() over every store, the plugin builders' set_config_store, and
what GET /config, MCP, config list, the audit entry and the config:changed
event say about each provider's store.

Ported from cli-server-node ``src/__tests__/config-multi-store.test.ts``."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any, Mapping, Optional

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli.config import CliConfigRegistry, CliConfigService
from qodalis_cli.create_cli_server import CliServerConfigOptions
from qodalis_cli.services.cli_event_bus import CliEventBus
from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder
from qodalis_cli_databases import CliDatabaseConfigStore, sqlite_connection
from qodalis_cli_jobs import CliJobsBuilder
from qodalis_cli_portal import CliPortalBuilder
from qodalis_cli_server_abstractions.audit import AuditEntry
from qodalis_cli_server_abstractions.config import (
    CliConfigError,
    CliConfigSectionDefinition,
    CliConfigSettingDefinition,
    CliConfigStoreDescription,
    CliConfigStoredSection,
    CliConfigUpdateResult,
    CliStoredConfigProvider,
    ICliConfigStore,
    InMemoryConfigStore,
    JsonFileConfigStore,
    cli_config_context,
    create_custom_config_provider,
)
from qodalis_cli_users import CliUsersBuilder

V1 = "/api/v1/qcli"
JWT_SECRET = "jwt-secret-for-tests-that-is-long-enough-for-hmac"
ALL = cli_config_context(None, "api")
MEMORY = {"kind": "memory", "title": "In memory", "detail": "", "persistent": False}


class NamedStore(InMemoryConfigStore):
    """A store recording its writes, described as ``name``."""

    def __init__(self, name: str, kind: str = "custom") -> None:
        super().__init__()
        self.name = name
        self.kind = kind
        self.writes: list[str] = []

    def describe(self) -> CliConfigStoreDescription:
        return CliConfigStoreDescription(
            kind=self.kind, title=self.name, detail=f"{self.name}-detail", persistent=self.kind != "memory",
        )

    async def write(self, provider, section, changes, expected_version=None) -> CliConfigStoredSection:
        self.writes.append(f"{provider}.{section}")
        return await super().write(provider, section, changes, expected_version)


def provider(
    id: str, store: Optional[ICliConfigStore] = None, applied: Optional[dict[str, Any]] = None,
) -> CliStoredConfigProvider:
    def apply(v: Any) -> None:
        if applied is not None:
            applied["limit"] = v

    return CliStoredConfigProvider(
        id=id,
        title=id[0].upper() + id[1:],
        store=store,
        sections=[CliConfigSectionDefinition(id="main", settings=[
            CliConfigSettingDefinition(key="limit", type="number", default=5, apply=apply),
            CliConfigSettingDefinition(key="name", type="string", default="x"),
        ])],
    )


class ReadOnly:
    def __init__(self, id: str) -> None:
        self.id = id
        self.title = id

    def get_sections(self, context: Any = None) -> list[dict[str, Any]]:
        return [{"id": "main", "entries": [{"key": "k", "type": "string", "value": "v"}]}]


def describe_of(r: CliConfigRegistry, provider_id: str) -> Optional[dict[str, Any]]:
    d = CliConfigService(r).describe_provider_store(provider_id)
    return d.to_dict() if d is not None else None


async def refused(awaitable: Any) -> CliConfigError:
    with pytest.raises(CliConfigError) as info:
        await awaitable
    return info.value


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestWhichStoreAProviderGets:
    def test_the_host_store_then_the_provider_own_then_the_default_store_then_memory(self):
        host_store, own_store, other_own, default_store = (
            NamedStore("host"), NamedStore("own"), NamedStore("other-own"), NamedStore("default"),
        )
        r = CliConfigRegistry()
        by_host = provider("byhost", own_store)
        by_own = provider("byown", other_own)
        by_default = provider("bydefault")
        r.add(by_host)
        r.add(by_own)
        r.add(by_default)
        r.add(ReadOnly("info"))

        # Without anything set: memory (the registry's default)
        assert r.store_of(by_default) is r.default_store
        assert r.default_store.source == "runtime"

        r.set_store(default_store)
        r.set_store(host_store, "byhost")
        assert r.store_of("byhost") is host_store
        assert r.store_of("byown") is other_own
        assert r.store_of("bydefault") is default_store
        assert r.store_of("info") is None
        assert r.store_of("missing") is None

        r.start()
        # What the providers were given
        assert r.store_of(by_host) is host_store
        assert by_host.own_store is own_store
        assert r.store_of(by_own) is other_own
        assert r.store_of(by_default) is default_store
        assert r.stores == [default_store, host_store, other_own]
        assert list(r.provider_stores) == ["byhost"]

    async def test_the_providers_are_attached_to_their_store_by_ready(self):
        host_store, own_store, default_store = NamedStore("host"), NamedStore("own"), NamedStore("default")
        r = CliConfigRegistry()
        r.set_store(default_store)
        r.set_store(host_store, "byhost")
        by_host, by_own, by_default = provider("byhost", NamedStore("ignored")), provider("byown", own_store), provider("bydefault")
        for p in (by_host, by_own, by_default):
            r.add(p)
        r.start()
        await r.ready()
        assert by_host.store is host_store
        assert by_own.store is own_store
        assert by_default.store is default_store

    async def test_a_provider_with_no_store_anywhere_keeps_its_changes_in_memory(self):
        r = CliConfigRegistry()
        r.add(provider("plain"))
        r.start()
        assert describe_of(r, "plain") == MEMORY
        # And a provider that is never registered uses its own store, else memory
        own = NamedStore("own")
        alone = provider("alone", own)
        await alone.update("main", {"limit": 7})
        assert own.writes == ["alone.main"]
        bare = provider("bare")
        await bare.update("main", {"limit": 8})
        assert bare.value("main", "limit") == 8

    async def test_the_provider_own_store_can_be_set_after_construction_not_once_registered(self):
        r = CliConfigRegistry()
        own = NamedStore("own")
        p = provider("later").set_store(own)
        assert p.own_store is own
        r.add(p)
        r.start()
        await r.ready()
        assert p.store is own
        with pytest.raises(RuntimeError, match="the config store of later must be set before the provider is registered"):
            p.set_store(NamedStore("too-late"))

    async def test_a_store_for_an_id_may_be_set_after_the_server_started_until_that_provider_is_registered(self):
        r = CliConfigRegistry()
        r.start()
        late = NamedStore("late")
        r.set_store(late, "plugin")
        p = provider("plugin")
        r.add(p)
        await r.ready()
        assert p.store is late
        with pytest.raises(RuntimeError, match="the config store of plugin must be set before the provider is registered"):
            r.set_store(NamedStore("again"), "plugin")
        with pytest.raises(RuntimeError, match="the config store must be set before the server is created"):
            r.set_store(NamedStore("default"))

    def test_refuses_a_store_without_read_and_write_and_an_invalid_provider_id(self):
        r = CliConfigRegistry()
        with pytest.raises(ValueError, match="invalid config store: it needs read and write"):
            r.set_store(object())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="invalid config provider id: Bad Id"):
            r.set_store(NamedStore("x"), "Bad Id")

    async def test_stored_values_are_applied_from_each_provider_own_store_when_the_server_starts(self):
        a, b = NamedStore("a"), NamedStore("b")
        await a.write("first", "main", {"limit": 1})
        await b.write("second", "main", {"limit": 2})
        # The same provider id in the other store is ignored
        await b.write("first", "main", {"limit": 99})
        applied_first: dict[str, Any] = {}
        applied_second: dict[str, Any] = {}
        r = CliConfigRegistry()
        r.set_store(a, "first")
        r.add(provider("first", None, applied_first))
        r.add(provider("second", b, applied_second))
        r.start()
        await r.ready()
        assert applied_first["limit"] == 1
        assert applied_second["limit"] == 2


# ---------------------------------------------------------------------------
# Providers in different stores, a shared store
# ---------------------------------------------------------------------------


class _Setup:
    def __init__(
        self,
        providers: list[Any],
        default: Optional[ICliConfigStore] = None,
        by_id: Optional[Mapping[str, ICliConfigStore]] = None,
    ) -> None:
        self.registry = CliConfigRegistry()
        if default is not None:
            self.registry.set_store(default)
        for provider_id, store in (by_id or {}).items():
            self.registry.set_store(store, provider_id)
        for p in providers:
            self.registry.add(p)
        self.registry.start()
        self.audits: list[AuditEntry] = []
        self.events: list[Any] = []
        bus = CliEventBus()
        bus.subscribe("config:*", self.events.append)

        async def sink(entry: AuditEntry) -> None:
            self.audits.append(entry)

        self.service = CliConfigService(self.registry, audit=lambda: sink, event_bus=lambda: bus)


class TestProvidersInDifferentStores:
    async def test_two_providers_in_two_stores_each_write_and_read_their_own(self, tmp_path):
        file = JsonFileConfigStore(tmp_path / "settings.json")
        db = sqlite3.connect(":memory:", check_same_thread=False)
        database = CliDatabaseConfigStore(sqlite_connection(db))
        s = _Setup([provider("jobs"), provider("users", file)], by_id={"jobs": database})
        await s.service.ready()

        await s.service.update("jobs", "main", {"changes": {"limit": 7}}, ALL)
        await s.service.update("users", "main", {"changes": {"limit": 3, "name": "u"}}, ALL)
        await s.service.update("jobs", "main", {"changes": {"name": "j"}}, ALL)

        # Each store holds only its provider
        assert await database.read("jobs", "main") == CliConfigStoredSection({"limit": 7, "name": "j"}, 2)
        assert await database.read("users", "main") == CliConfigStoredSection({}, 0)
        assert json.loads((tmp_path / "settings.json").read_text("utf-8")) == {
            "users": {"main": {"values": {"limit": 3, "name": "u"}, "version": 1}},
        }
        rows = db.execute('SELECT "Provider", "Section", "Version" FROM "QcliConfig"').fetchall()
        assert rows == [("jobs", "main", 2)]

        # And each reads back its own values and version
        jobs = await s.service.get("jobs", ALL)
        users = await s.service.get("users", ALL)
        assert [(e["key"], e["value"], e["source"]) for e in jobs["sections"][0]["entries"]] == [
            ("limit", 7, "database"), ("name", "j", "database"),
        ]
        assert jobs["sections"][0]["version"] == "2"
        assert [(e["key"], e["value"], e["source"]) for e in users["sections"][0]["entries"]] == [
            ("limit", 3, "file"), ("name", "u", "file"),
        ]
        assert users["sections"][0]["version"] == "1"
        assert jobs["store"] == {"kind": "database", "title": "Database", "detail": "SQLite, table QcliConfig", "persistent": True}
        assert users["store"] == {"kind": "file", "title": "JSON file", "detail": "settings.json", "persistent": True}
        db.close()

    async def test_one_store_shared_by_two_providers_keeps_their_data_apart(self):
        shared = NamedStore("shared")
        s = _Setup([provider("alpha"), provider("beta", shared), provider("gamma")], by_id={"alpha": shared})
        await s.service.update("alpha", "main", {"changes": {"limit": 1}}, ALL)
        await s.service.update("beta", "main", {"changes": {"limit": 2}}, ALL)
        await s.service.update("beta", "main", {"changes": {"limit": 3}}, ALL)
        assert shared.writes == ["alpha.main", "beta.main", "beta.main"]
        assert await shared.read("alpha", "main") == CliConfigStoredSection({"limit": 1}, 1)
        assert await shared.read("beta", "main") == CliConfigStoredSection({"limit": 3}, 2)
        # gamma uses the default (memory): untouched by the others
        assert (await s.service.get("gamma", ALL))["sections"][0]["version"] == "0"
        assert len([x for x in s.registry.stores if x is shared]) == 1
        views = {v["id"]: v for v in await s.service.list(ALL)}
        assert views["alpha"]["store"] == views["beta"]["store"]

    async def test_versions_and_conflicts_are_per_store(self):
        left, right = NamedStore("left"), NamedStore("right")
        s = _Setup([provider("left"), provider("right")], default=left, by_id={"right": right})
        await s.service.update("left", "main", {"changes": {"limit": 1}, "version": "0"}, ALL)
        await s.service.update("left", "main", {"changes": {"limit": 2}, "version": "1"}, ALL)
        # right is still at version 0 in its own store
        err = await refused(s.service.update("right", "main", {"changes": {"limit": 9}, "version": "2"}, ALL))
        assert (err.status, err.version) == (409, "0")
        await s.service.update("right", "main", {"changes": {"limit": 9}, "version": "0"}, ALL)
        # A stale version against left is refused with left's version, right unaffected
        err = await refused(s.service.update("left", "main", {"changes": {"limit": 3}, "version": "1"}, ALL))
        assert (err.status, err.version) == (409, "2")
        # A store refusing on its own (another server wrote meanwhile) is a 409 too
        await right.write("right", "main", {"name": "by-another-server"})
        err = await refused(s.service.update("right", "main", {"changes": {"limit": 4}, "version": "1"}, ALL))
        assert (err.status, err.version) == (409, "2")
        assert await left.read("left", "main") == CliConfigStoredSection({"limit": 2}, 2)
        assert await right.read("right", "main") == CliConfigStoredSection({"limit": 9, "name": "by-another-server"}, 2)
        # Only successful writes are audited and published
        assert [f"{a.properties['provider']}@{a.properties['version']}" for a in s.audits] == [
            "left@1", "left@2", "right@1",
        ]
        assert len(s.events) == 3

    async def test_the_audit_entry_and_the_event_name_the_kind_of_store_written(self, tmp_path):
        file = JsonFileConfigStore(tmp_path / "c.json")
        custom = NamedStore("vault", "custom")
        s = _Setup([provider("plain"), provider("secret")], default=file, by_id={"secret": custom})
        await s.service.update("plain", "main", {"changes": {"limit": 2}}, ALL)
        await s.service.update("secret", "main", {"changes": {"limit": 3}}, ALL)
        assert [a.properties["store"] for a in s.audits] == ["file", "custom"]
        assert [e.data["store"] for e in s.events] == ["file", "custom"]
        # The kind only: never the detail (paths, hosts)
        assert "c.json" not in json.dumps([a.properties for a in s.audits])
        assert "vault-detail" not in json.dumps([e.data for e in s.events])
        assert s.audits[0].properties == {
            "provider": "plain", "section": "main", "version": "1", "store": "file", "via": "api",
            "changes": [{"key": "limit", "old": 5, "new": 2}],
        }
        assert s.events[1].data == {
            "provider": "secret", "section": "main", "keys": ["limit"], "version": "1",
            "requiresRestart": False, "store": "custom",
        }

    async def test_a_writer_that_saves_itself_is_audited_without_a_store(self):
        class SelfSaving:
            id = "self"
            title = "Self"

            def get_sections(self, context: Any = None) -> list[dict[str, Any]]:
                return [{"id": "main", "version": "1", "entries": [
                    {"key": "k", "type": "string", "value": "v", "mutable": True},
                ]}]

            def validate(self, section_id: str, changes: Any, context: Any = None) -> dict[str, str]:
                return {}

            async def update(self, section_id: str, changes: Any, expected_version: Any = None, context: Any = None):
                return CliConfigUpdateResult(applied=["k"], requires_restart=False, version="2")

        s = _Setup([SelfSaving()])
        await s.service.update("self", "main", {"changes": {"k": "w"}}, ALL)
        assert "store" not in s.audits[0].properties
        assert "store" not in s.events[0].data
        assert "store" not in await s.service.get("self", ALL)


# ---------------------------------------------------------------------------
# ready()
# ---------------------------------------------------------------------------


class TestReadyOverEveryStore:
    async def test_waits_for_each_store_own_ready_and_every_provider_loading_from_its_store(self):
        loop = asyncio.get_running_loop()
        slow = NamedStore("slow")
        slow.ready = loop.create_future()  # type: ignore[attr-defined]
        called: list[bool] = []
        with_fn = NamedStore("fn")

        async def fn_ready() -> None:
            called.append(True)

        with_fn.ready = fn_ready  # type: ignore[attr-defined]
        cli = create_cli_server(CliServerOptions(
            config=CliServerConfigOptions(store=slow, stores={"fn": with_fn}),
            configure=lambda b: b.add_config_provider(provider("fn")),
        ))
        task = asyncio.ensure_future(cli.config.ready())
        await asyncio.sleep(0.01)
        assert not task.done()
        slow.ready.set_result(None)  # type: ignore[attr-defined]
        await task
        assert called == [True]
        # Again: a resolved future is awaited as many times as needed
        await cli.config.ready()

    async def test_raises_when_one_of_the_stores_fails_even_one_only_a_single_provider_uses(self):
        class Failing(ICliConfigStore):
            source = "database"

            async def read(self, provider, section):
                raise RuntimeError("redis down")

            async def write(self, provider, section, changes, expected_version=None):
                raise RuntimeError("redis down")

        cli = create_cli_server(CliServerOptions(
            config=CliServerConfigOptions(stores={"jobs": Failing()}),
            configure=lambda b: b.add_config_provider(provider("jobs")),
        ))
        with pytest.raises(RuntimeError, match="redis down"):
            await cli.config.ready()

    async def test_raises_when_a_store_is_not_ready(self):
        class NotReady(InMemoryConfigStore):
            async def ready(self) -> None:
                raise RuntimeError("no table")

        cli = create_cli_server(CliServerOptions(config=CliServerConfigOptions(stores={"jobs": NotReady()})))
        cli.builder.add_config_provider(provider("jobs"))
        with pytest.raises(RuntimeError, match="no table"):
            await cli.config.ready()


# ---------------------------------------------------------------------------
# The server: options, builders, API, MCP, terminal
# ---------------------------------------------------------------------------


class _Server:
    def __init__(self, client: TestClient, token: str, db: sqlite3.Connection, dir: Any,
                 jobs_own: NamedStore, logs_store: NamedStore, redis_like: NamedStore) -> None:
        self.c, self.token, self.db, self.dir = client, token, db, dir
        self.jobs_own, self.logs_store, self.redis_like = jobs_own, logs_store, redis_like

    @property
    def h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


FILE_SECTION = {"kind": "file", "title": "JSON file", "detail": "app.json, section qcli", "persistent": True}
REDIS_LIKE = {"kind": "redis", "title": "Redis", "detail": "Redis-detail", "persistent": True}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    directory = tmp_path_factory.mktemp("qcli-multi-server")
    (directory / "app.json").write_text(json.dumps({"app": {"name": "demo"}}, indent=2))
    db = sqlite3.connect(":memory:", check_same_thread=False)
    database = CliDatabaseConfigStore(sqlite_connection(db), database="qcli-demo.sqlite")
    jobs_own, logs_store, redis_like = NamedStore("jobs-own"), NamedStore("logs-store", "custom"), NamedStore("Redis", "redis")
    result = create_cli_server(CliServerOptions(
        mcp=True,
        config=CliServerConfigOptions(
            store=JsonFileConfigStore(directory / "app.json", section="qcli"),
            # jobs: the host's store wins over the one the jobs builder sets
            stores={"jobs": database, "custom": redis_like},
        ),
        configure=lambda b: b.add_config_provider(provider("billing")).set_config_store(redis_like, "billing"),
    ))
    admin = (
        CliAdminBuilder().set_credentials("admin", "adminpass").set_jwt_secret(JWT_SECRET).set_config_store(logs_store)
        .build(AdminBuildDeps(registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder))
    )
    result.mount_plugin(CliJobsBuilder().set_config_store(jobs_own).build())
    result.mount_plugin(admin)
    try:
        with TestClient(result.app) as client:
            res = client.post(f"{V1}/auth/login", json={"username": "admin", "password": "adminpass"})
            assert res.status_code == 200, res.text
            yield _Server(client, res.json()["token"], db, directory, jobs_own, logs_store, redis_like)
    finally:
        admin.log_buffer.uninstall_handler()
        db.close()


class TestAServerWithProvidersInSeveralStores:
    def test_get_config_default_store_and_a_store_on_every_stored_provider(self, server: _Server):
        res = server.c.get(f"{V1}/config", headers=server.h)
        assert res.status_code == 200
        body = res.json()
        assert sorted(body) == ["defaultStore", "providers"]
        assert body["defaultStore"] == FILE_SECTION
        stores = {p["id"]: p.get("store") for p in body["providers"]}
        assert stores == {
            "server": None,
            "auth": None,
            "host": None,
            "cors": FILE_SECTION,
            "mcp": FILE_SECTION,
            "logs": {"kind": "custom", "title": "logs-store", "detail": "logs-store-detail", "persistent": True},
            # the dashboard plugins' settings: no store of their own, the default one
            "dashboard-plugins": FILE_SECTION,
            "jobs": {
                "kind": "database", "title": "Database",
                "detail": "SQLite database qcli-demo.sqlite, table QcliConfig", "persistent": True,
            },
            "billing": REDIS_LIKE,
            "custom": REDIS_LIKE,
        }
        # Every provider keeps its storage; a stored one says store
        jobs = next(p for p in body["providers"] if p["id"] == "jobs")
        assert jobs["storage"] == {"kind": "store", "description": "Changes are saved to its config store", "sources": []}
        assert list(jobs) == ["id", "title", "description", "order", "writable", "storage", "store", "sections"]

    def test_get_config_provider_with_its_store(self, server: _Server):
        res = server.c.get(f"{V1}/config/jobs", headers=server.h)
        assert res.status_code == 200
        assert res.json()["store"]["kind"] == "database"
        res = server.c.get(f"{V1}/config/server", headers=server.h)
        assert res.status_code == 200
        assert "store" not in res.json()

    def test_patch_writes_to_the_provider_own_store_only_and_the_rest_of_the_settings_file_stays(self, server: _Server):
        for path, body in (
            ("jobs/history", {"changes": {"maxRunsPerJob": 25}, "version": "0"}),
            ("logs/buffer", {"changes": {"minLevel": "WARN"}}),
            ("custom/custom", {"changes": {"motd": "hi"}}),
            ("cors/cors", {"changes": {"allowedOrigins": ["https://a.example"]}}),
        ):
            res = server.c.patch(f"{V1}/config/{path}", headers=server.h, json=body)
            assert res.status_code == 200, (path, res.text)
        rows = server.db.execute('SELECT "Provider", "Section", "Data" FROM "QcliConfig"').fetchall()
        assert [(p, s, json.loads(d)) for p, s, d in rows] == [("jobs", "history", {"maxRunsPerJob": 25})]
        assert server.jobs_own.writes == []
        assert server.logs_store.writes == ["logs.buffer"]
        assert server.redis_like.writes == ["custom.custom"]
        file = json.loads((server.dir / "app.json").read_text("utf-8"))
        assert file["app"] == {"name": "demo"}
        assert list(file["qcli"]) == ["cors"]

    def test_mcp_config_list_and_config_get_carry_each_provider_store(self, server: _Server):
        def rpc(name: str, args: Optional[dict[str, Any]] = None) -> dict[str, Any]:
            res = server.c.post("/api/qcli/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}},
            })
            return res.json()["result"]["structuredContent"]

        listed = rpc("config_list")
        assert listed["defaultStore"]["detail"] == "app.json, section qcli"
        assert "store" not in listed
        providers = {p["id"]: p for p in listed["providers"]}
        assert providers["jobs"]["store"]["kind"] == "database"
        assert "store" not in providers["server"]
        assert rpc("config_get", {"provider": "billing"})["provider"]["store"] == REDIS_LIKE

    def test_config_list_the_default_store_first_then_each_stored_provider_with_its_store(self, server: _Server):
        res = server.c.post(f"{V1}/execute", headers=server.h, json={
            "command": "config", "chainCommands": ["list"], "rawCommand": "config list",
        })
        text = "\n".join(str(o.get("value")) for o in res.json().get("outputs") or [])
        heads = [line for line in text.split("\n") if not line.startswith("  ")]
        assert heads == [
            "Default store: JSON file (app.json, section qcli), kept across restarts",
            "server  Server  (read-only)",
            "auth  Authentication  (read-only)",
            "host  Host  (read-only; from Server options, Environment variables)",
            "cors  CORS  · JSON file (app.json, section qcli)",
            "mcp  MCP  · JSON file (app.json, section qcli)",
            "jobs  Jobs  · Database (SQLite database qcli-demo.sqlite, table QcliConfig)",
            "logs  Logs  · logs-store (logs-store-detail)",
            "dashboard-plugins  Dashboard plugins  · JSON file (app.json, section qcli)",
            "billing  Billing  · Redis (Redis-detail)",
            "custom  Custom  · Redis (Redis-detail)",
        ]


class TestOptionsAndBuilderAreTheSame:
    def test_both_set_the_store_of_one_provider_and_create_custom_config_provider_takes_a_store(self):
        a, b = NamedStore("a"), NamedStore("b")
        via_options = create_cli_server(CliServerOptions(
            config=CliServerConfigOptions(stores={"billing": a}),
            configure=lambda x: x.add_config_provider(provider("billing")),
        ))
        via_builder = create_cli_server(CliServerOptions(
            configure=lambda x: x.set_config_store(b, "billing").add_config_provider(provider("billing")),
        ))
        assert via_options.config.describe_provider_store("billing").title == "a"
        assert via_builder.config.describe_provider_store("billing").title == "b"
        own = NamedStore("own")
        assert create_custom_config_provider(id="extra", store=own).own_store is own


class TestPluginBuildersGiveTheirProviderItsOwnStore:
    def test_jobs_users_portal_and_admin_set_the_provider_own_store_none_by_default(self):
        store = NamedStore("plugin")

        def own(providers: Any, provider_id: str) -> Any:
            return next(p for p in providers or [] if p.id == provider_id).own_store

        jobs = CliJobsBuilder().set_config_store(store).build()
        users = CliUsersBuilder().set_default_admin_password("rootpass1").set_config_store(store).build()
        portal = CliPortalBuilder().set_config_store(store).build()
        try:
            assert own(jobs.config_providers, "jobs") is store
            assert own(users.config_providers, "users") is store
            assert own(portal.config_providers, "portal") is store
        finally:
            users.dispose()
        assert own(CliJobsBuilder().build().config_providers, "jobs") is None

        cli = create_cli_server(CliServerOptions())
        admin = (
            CliAdminBuilder().set_credentials("admin", "adminpass").set_jwt_secret(JWT_SECRET).set_config_store(store)
            .build(AdminBuildDeps(registry=cli.registry, event_socket_manager=cli.event_socket_manager, builder=cli.builder))
        )
        try:
            assert cli.config.describe_provider_store("logs").title == "plugin"
            assert cli.config.describe_provider_store("auth") is None
        finally:
            admin.log_buffer.uninstall_handler()
