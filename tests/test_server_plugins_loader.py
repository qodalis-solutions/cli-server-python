"""Server plugins loaded through their entry point: installed at runtime (``<data dir>/server-plugins``, the fixture
plugin as pip leaves it) and added in code (``builder.add_plugin``), their order, settings (code defaults, origin,
needs-configuration), failures kept apart, and the official entries."""

from __future__ import annotations

import json
import os
import stat
from typing import Any

import pytest
from fastapi.testclient import TestClient

import qodalis_cli_server_abstractions as abstractions
from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli_server_abstractions import (
    CLI_PLUGIN_LOAD_ORDER,
    CliPluginEntryInfo,
    CliPluginNeedsConfiguration,
)
from qodalis_cli_server_abstractions.config import (
    CliConfigSettingDefinition,
    CliConfigValidation,
    InMemoryConfigStore,
)

from .command_helpers import V1, run_command
from .server_plugin_helpers import (
    HELLO_PACKAGE,
    forget_fixture_modules,
    hello_record,
    install_fixture,
    read_installed,
    write_installed,
)


@pytest.fixture(autouse=True)
def _clean_modules():
    forget_fixture_modules()
    yield
    forget_fixture_modules()


def server(tmp_path: Any, configure: Any = None, **options: Any) -> Any:
    return create_cli_server(CliServerOptions(data_dir=str(tmp_path / "data"), configure=configure, **options))


def plugins_dir(tmp_path: Any) -> str:
    return str(tmp_path / "data" / "server-plugins")


def states(result: Any) -> dict[str, dict[str, Any]]:
    return {s.name: s.to_dict() for s in result.server_plugins.states()}


def config_get(client: Any, result: Any, provider_id: str) -> dict[str, Any]:
    """The config view of a provider, as ``GET /config/{id}`` answers it (the config service, no sign-in)."""
    from qodalis_cli_server_abstractions.config import cli_config_context

    return client.portal.call(result.builder.config.get, provider_id, cli_config_context(None))


def config_set(client: Any, result: Any, provider_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    from qodalis_cli_server_abstractions.config import cli_config_context

    return client.portal.call(result.builder.config.update, provider_id, "settings", {"changes": changes}, cli_config_context(None))


def code_entry(name: str, fn: Any, order: int | None = None, package: str | None = None) -> Any:
    fn.plugin = CliPluginEntryInfo(id=name, package=package or f"acme-{name}")
    if order is not None:
        fn.order = order
    return fn


# --------------------------------------------------------------------------- installed at runtime


def test_an_installed_plugin_loads_with_its_settings_command_route_and_data_dir(tmp_path):
    install_fixture(plugins_dir(tmp_path))
    write_installed(plugins_dir(tmp_path), [hello_record(title="Hello", icon="👋")])
    result = server(tmp_path)
    with TestClient(result.app) as client:
        assert states(result)["hello"] == {
            "name": "hello", "package": HELLO_PACKAGE, "version": "1.2.0", "status": "loaded", "source": "marketplace",
            "order": 50, "settings": "hello",
        }
        body = client.get(f"{V1}/hello").json()
        assert body["greeting"] == "Hello"
        assert body["dataDir"] == str(tmp_path / "data" / "plugin-data" / "hello")
        assert os.path.isdir(body["dataDir"])
        assert run_command(client, result, "hello bob").text == "Hello, bob!"
        listed = next(p for p in result.builder.installed_plugins if p.id == HELLO_PACKAGE)
        assert (listed.name, listed.source, listed.icon, listed.settings) == ("Hello", "marketplace", "👋", "hello")
        # The plugin imports the server's abstractions, never a copy of its own
        import qcli_hello_plugin.plugin as hello

        assert hello.ABSTRACTIONS_FILE == abstractions.__file__
        assert hello.last_context.source == "marketplace" and hello.last_context.options == {}
    # Its settings are kept in the plugins' settings file (the server keeps its own in memory), mode 0600
    path = tmp_path / "data" / "plugin-settings.json"
    assert not path.exists() or stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert read_installed(plugins_dir(tmp_path))[0]["settings"] == "hello"


def test_saved_settings_survive_a_restart_in_the_plugins_settings_file(tmp_path):
    install_fixture(plugins_dir(tmp_path))
    write_installed(plugins_dir(tmp_path), [hello_record()])
    result = server(tmp_path)
    with TestClient(result.app) as client:
        config_set(client, result, "hello", {"greeting": "Salut"})
        assert client.get(f"{V1}/hello").json()["greeting"] == "Salut"
    path = tmp_path / "data" / "plugin-settings.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    forget_fixture_modules()
    again = server(tmp_path)
    with TestClient(again.app) as client:
        assert client.get(f"{V1}/hello").json()["greeting"] == "Salut"


def test_a_failing_plugin_is_kept_apart_and_its_error_in_installed_json_then_cleared(tmp_path):
    install_fixture(plugins_dir(tmp_path), entry_points="[qodalis_cli.plugins]\nhello = qcli_hello_plugin.missing:create_plugin\n")
    write_installed(plugins_dir(tmp_path), [hello_record()])
    result = server(tmp_path)
    with TestClient(result.app) as client:
        assert states(result)["hello"]["status"] == "failed"
        assert "qcli_hello_plugin.missing" in states(result)["hello"]["error"]
        assert client.get(f"{V1}/version").status_code == 200
        assert all(p.id != HELLO_PACKAGE for p in result.builder.installed_plugins)
    assert "error" in read_installed(plugins_dir(tmp_path))[0]
    forget_fixture_modules()
    install_fixture(plugins_dir(tmp_path))
    fixed = server(tmp_path)
    with TestClient(fixed.app):
        assert states(fixed)["hello"]["status"] == "loaded"
    assert "error" not in read_installed(plugins_dir(tmp_path))[0]


@pytest.mark.parametrize("record,message", [
    (hello_record(version="9.9.9"), "the installed version is 1.2.0, not 9.9.9"),
    (hello_record(package="qcli-missing"), "not installed in"),
])
def test_records_that_do_not_match_the_directory_fail(tmp_path, record, message):
    install_fixture(plugins_dir(tmp_path))
    write_installed(plugins_dir(tmp_path), [record])
    result = server(tmp_path)
    with TestClient(result.app):
        assert message in states(result)["hello"]["error"]


def test_a_package_without_the_entry_point_fails(tmp_path):
    install_fixture(plugins_dir(tmp_path), entry_points="")
    write_installed(plugins_dir(tmp_path), [hello_record()])
    result = server(tmp_path)
    with TestClient(result.app):
        assert states(result)["hello"]["error"] == "not a server plugin (no qodalis_cli.plugins entry point)"


def test_a_package_of_the_servers_environment_is_skipped_unless_enabled_as_bundled(tmp_path):
    write_installed(plugins_dir(tmp_path), [{
        "name": "weather", "package": "qodalis-cli-server-plugin-weather", "version": "1.0.0b31", "integrity": "",
    }])
    result = server(tmp_path)
    with TestClient(result.app):
        assert states(result)["weather"]["status"] == "skipped"
        assert states(result)["weather"]["error"].startswith("installed in the server's environment")
    write_installed(plugins_dir(tmp_path), [{
        "name": "weather", "package": "qodalis-cli-server-plugin-weather", "version": "1.0.0b31", "integrity": "", "bundled": True,
    }])
    bundled = server(tmp_path)
    with TestClient(bundled.app) as client:
        assert states(bundled)["weather"]["status"] == "loaded", states(bundled)
        assert "London" in json.dumps(run_command(client, bundled, "weather --help").text) or bundled.registry.find_processor("weather")


def test_a_broken_installed_json_loads_nothing_and_the_server_starts(tmp_path):
    os.makedirs(plugins_dir(tmp_path))
    with open(os.path.join(plugins_dir(tmp_path), "installed.json"), "w") as f:
        f.write("{not json")
    result = server(tmp_path)
    with TestClient(result.app) as client:
        assert result.server_plugins.states() == []
        assert client.get(f"{V1}/version").status_code == 200


def test_server_plugins_false_ignores_the_installed_ones_but_loads_code_plugins(tmp_path):
    install_fixture(plugins_dir(tmp_path))
    write_installed(plugins_dir(tmp_path), [hello_record()])
    loaded: list[str] = []

    def configure(b: Any) -> None:
        b.add_plugin(code_entry("mine", lambda c: loaded.append(c.plugin_id)))

    result = server(tmp_path, configure, server_plugins=False)
    with TestClient(result.app):
        assert list(states(result)) == ["mine"] and loaded == ["mine"]


def test_routes_of_a_late_plugin_come_before_a_host_catch_all(tmp_path):
    install_fixture(plugins_dir(tmp_path))
    write_installed(plugins_dir(tmp_path), [hello_record()])
    result = server(tmp_path)

    @result.app.get("/{path:path}")
    async def catch_all(path: str) -> dict[str, str]:
        return {"spa": path}

    with TestClient(result.app) as client:
        assert client.get(f"{V1}/hello").json()["greeting"] == "Hello"
        assert client.get("/anything").json() == {"spa": "anything"}


# --------------------------------------------------------------------------- added in code


def test_plugins_load_in_their_declared_order_code_first_on_a_tie(tmp_path):
    install_fixture(plugins_dir(tmp_path))
    write_installed(plugins_dir(tmp_path), [hello_record()])
    seen: list[str] = []

    def configure(b: Any) -> None:
        b.add_plugin(code_entry("source", lambda c: seen.append("source"), CLI_PLUGIN_LOAD_ORDER.SOURCE))
        b.add_plugin(code_entry("plain", lambda c: seen.append("plain")))
        b.add_plugin(code_entry("auth", lambda c: seen.append("auth"), CLI_PLUGIN_LOAD_ORDER.AUTH))
        b.add_plugin(code_entry("store", lambda c: seen.append("store"), CLI_PLUGIN_LOAD_ORDER.CONFIG_STORE))
        b.add_plugin(code_entry("core", lambda c: seen.append("core"), CLI_PLUGIN_LOAD_ORDER.CORE))

    result = server(tmp_path, configure)
    with TestClient(result.app):
        assert seen == ["auth", "store", "core", "plain", "source"]
        assert [s.name for s in result.server_plugins.states()] == ["auth", "store", "core", "plain", "hello", "source"]
        assert {s.name: s.source for s in result.server_plugins.states()}["plain"] == "code"


@pytest.mark.asyncio
async def test_code_options_are_the_defaults_saved_settings_override_them_and_each_value_says_its_origin(tmp_path):
    got: dict[str, Any] = {}

    async def entry(context: Any) -> None:
        provider = await context.settings(settings=[
            CliConfigSettingDefinition(key="greeting", type="string", default="Hello"),
            CliConfigSettingDefinition(key="times", type="number", default=1),
            CliConfigSettingDefinition(key="loud", type="boolean", default=False),
        ])
        got["values"] = {k: provider.value("settings", k) for k in ("greeting", "times", "loud")}
        got["pool"] = context.options.get("pool")
        got["origin"] = {k: provider.origin("settings", k) for k in ("greeting", "times", "loud")}

    pool = object()
    store = InMemoryConfigStore()
    await store.write("greeter", "settings", {"times": 3}, None)

    def configure(b: Any) -> None:
        b.set_config_store(store)
        b.add_plugin(code_entry("greeter", entry), {"greeting": "Hi", "times": 2, "pool": pool})

    result = server(tmp_path, configure)
    with TestClient(result.app) as client:
        assert got["values"] == {"greeting": "Hi", "times": 3, "loud": False}
        assert got["pool"] is pool
        assert got["origin"] == {"greeting": "code", "times": "setting", "loud": "default"}
        root = config_get(client, result, "greeter")
        view = {e["key"]: (e["value"], e["source"], e["origin"]) for s in root["sections"] for e in s["entries"]}
        assert view == {"greeting": ("Hi", "code", "code"), "times": (3, "runtime", "setting"), "loud": (False, "default", "default")}
        listed = next(p for p in result.builder.installed_plugins if p.id == "acme-greeter")
        assert (listed.source, listed.settings) == ("code", "greeter")
        res = client.get(f"{V1}/plugins/installed")
        if res.status_code == 200:
            item = next(p for p in res.json() if p["id"] == "acme-greeter")
            assert item["source"] == "code" and item["settings"] == "greeter"


def test_required_settings_without_a_value_make_the_plugin_need_configuration(tmp_path):
    registered: list[str] = []

    async def entry(context: Any) -> Any:
        provider = await context.settings(settings=[
            CliConfigSettingDefinition(key="url", type="secret", required=True),
            CliConfigSettingDefinition(key="name", type="string", default="", validation=CliConfigValidation(required=True)),
        ])
        if provider.missing_required():
            return context.needs_configuration(provider.missing_required())
        registered.append("module")
        return None

    async def raising(context: Any) -> Any:
        raise CliPluginNeedsConfiguration("an API key")

    def configure(b: Any) -> None:
        b.add_plugin(code_entry("remote", entry))
        b.add_plugin(code_entry("other", raising))

    result = server(tmp_path, configure)
    with TestClient(result.app) as client:
        assert states(result)["remote"]["status"] == "needs-configuration"
        assert states(result)["remote"]["missing"] == ["settings.url", "settings.name"]
        assert states(result)["other"]["missing"] == ["an API key"]
        assert registered == []
        listed = next(p for p in result.builder.installed_plugins if p.id == "acme-remote")
        assert (listed.status, list(listed.missing or [])) == ("needs-configuration", ["settings.url", "settings.name"])
        view = config_get(client, result, "remote")
        assert [e.get("required") for s in view["sections"] for e in s["entries"]] == [True, None]


def test_a_plugin_installed_at_runtime_needing_configuration_is_marked_in_installed_json(tmp_path):
    install_fixture(plugins_dir(tmp_path))
    write_installed(plugins_dir(tmp_path), [hello_record()])

    def configure(b: Any) -> None:
        b.set_config_store(InMemoryConfigStore())

    result = server(tmp_path, configure)
    with TestClient(result.app):
        assert states(result)["hello"]["status"] == "loaded"
    record = read_installed(plugins_dir(tmp_path))[0]
    assert "status" not in record and record["settings"] == "hello"


def test_add_plugin_refuses_an_entry_without_plugin_info_twice_or_after_loading(tmp_path):
    result = server(tmp_path)
    with pytest.raises(ValueError, match="names no plugin"):
        result.builder.add_plugin(lambda c: None)
    result.builder.add_plugin(code_entry("one", lambda c: None))
    with pytest.raises(ValueError, match="added twice"):
        result.builder.add_plugin(code_entry("one-again", lambda c: None, package="acme-one"))
    with TestClient(result.app):
        with pytest.raises(RuntimeError, match="already loaded"):
            result.builder.add_plugin(code_entry("late", lambda c: None))


def test_a_marketplace_record_of_a_package_added_in_code_is_skipped(tmp_path):
    install_fixture(plugins_dir(tmp_path))
    write_installed(plugins_dir(tmp_path), [hello_record()])

    def configure(b: Any) -> None:
        b.add_plugin(code_entry("hello-code", lambda c: None, package=HELLO_PACKAGE))

    result = server(tmp_path, configure)
    with TestClient(result.app):
        assert states(result)["hello"] == {**states(result)["hello"], "status": "skipped", "error": "already installed by the host"}


def test_a_config_store_plugin_moves_the_settings_of_plugins_loaded_before_it(tmp_path):
    store = InMemoryConfigStore()
    seen: dict[str, Any] = {}

    async def auth(context: Any) -> None:
        provider = await context.settings(settings=[CliConfigSettingDefinition(key="mode", type="string", default="open")])
        seen["auth"] = provider

    def config_store(context: Any) -> None:
        context.builder.replace_config_store(store)

    def configure(b: Any) -> None:
        b.add_plugin(code_entry("auth", auth, CLI_PLUGIN_LOAD_ORDER.AUTH))
        b.add_plugin(code_entry("store", config_store, CLI_PLUGIN_LOAD_ORDER.CONFIG_STORE))

    result = server(tmp_path, configure)
    with TestClient(result.app) as client:
        assert result.builder.config_registry.store_of("auth") is store
        config_set(client, result, "auth", {"mode": "closed"})
    assert not (tmp_path / "data" / "plugin-settings.json").exists()


def test_without_a_config_store_the_parked_settings_go_to_the_settings_file(tmp_path):
    async def auth(context: Any) -> None:
        await context.settings(settings=[CliConfigSettingDefinition(key="mode", type="string", default="open")])

    def unconfigured_store(context: Any) -> Any:
        return context.needs_configuration("settings.url")

    def configure(b: Any) -> None:
        b.add_plugin(code_entry("auth", auth, CLI_PLUGIN_LOAD_ORDER.AUTH))
        b.add_plugin(code_entry("store", unconfigured_store, CLI_PLUGIN_LOAD_ORDER.CONFIG_STORE))

    result = server(tmp_path, configure)
    with TestClient(result.app):
        file = result.server_plugins.settings_file()
        assert result.builder.config_registry.store_of("auth") is file


# --------------------------------------------------------------------------- the official entries


def test_weather_from_code_takes_its_default_place_as_a_code_setting(tmp_path):
    from qodalis_cli_weather import plugin as weather

    result = server(tmp_path, lambda b: b.add_plugin(weather, {"defaultLocation": "Paris"}))
    with TestClient(result.app) as client:
        assert states(result)["weather"]["status"] == "loaded"
        view = config_get(client, result, "weather")
        entry = view["sections"][0]["entries"][0]
        assert (entry["value"], entry["origin"]) == ("Paris", "code")
        processor = result.registry.find_processor("weather")
        assert "Paris" in processor.documentation


def test_filesystem_needs_its_folders_then_serves_them_read_only(tmp_path):
    from qodalis_cli_filesystem import plugin as filesystem

    first = server(tmp_path, lambda b: b.add_plugin(filesystem))
    with TestClient(first.app):
        assert states(first)["filesystem"]["status"] == "needs-configuration"
        assert states(first)["filesystem"]["missing"] == ["settings.roots"]
        assert first.builder.file_storage_provider is None
    root = tmp_path / "files"
    root.mkdir()
    (root / "a.txt").write_text("hi")
    second = create_cli_server(CliServerOptions(
        data_dir=str(tmp_path / "data2"), configure=lambda b: b.add_plugin(filesystem, {"roots": [str(root)]}),
    ))
    with TestClient(second.app) as client:
        assert states(second)["filesystem"]["status"] == "loaded"
        listing = client.get("/api/qcli/fs/ls", params={"path": str(root)})
        assert listing.status_code == 200, listing.text
        upload = client.post("/api/qcli/fs/upload", data={"path": str(root / "b.txt")}, files={"file": ("b.txt", b"x", "text/plain")})
        assert upload.status_code == 403
        assert not (root / "b.txt").exists()


def test_a_data_explorer_source_added_at_runtime_follows_the_data_explorer_rules(tmp_path):
    from qodalis_cli_data_explorer import plugin as data_explorer
    from qodalis_cli_data_explorer_sql import plugin as sql

    def configure(b: Any) -> None:
        b.add_plugin(sql, {"filename": ":memory:", "readOnly": False, "maxRows": 500})
        b.add_plugin(data_explorer, {"forceReadOnly": True, "maxRows": 100})

    result = server(tmp_path, configure)
    with TestClient(result.app) as client:
        assert [s.name for s in result.server_plugins.states()] == ["data-explorer", "data-explorer-sql"]
        options = result.builder.data_explorer_registrations[0].options
        assert (options.name, options.read_only, options.max_rows) == ("sql", True, 100)
        sources = client.get("/api/qcli/data-explorer/sources")
        assert sources.status_code == 200 and [s["name"] for s in sources.json()] == ["sql"]


def test_sources_with_a_connection_need_configuration_until_it_is_set(tmp_path):
    from qodalis_cli_data_explorer_postgres import plugin as postgres

    result = server(tmp_path, lambda b: b.add_plugin(postgres))
    with TestClient(result.app):
        assert states(result)["data-explorer-postgres"]["missing"] == ["settings.connectionString"]
        assert result.builder.data_explorer_registrations == []


def test_users_then_public_api_then_portal_build_on_each_other(tmp_path):
    from qodalis_cli_docs import plugin as docs
    from qodalis_cli_portal import plugin as portal
    from qodalis_cli_public_api import plugin as public_api
    from qodalis_cli_users import plugin as users

    def configure(b: Any) -> None:
        b.add_plugin(portal)
        b.add_plugin(public_api)
        b.add_plugin(docs)
        b.add_plugin(users, {"defaultAdminPassword": "rootpass1", "min_password_length": 4})

    result = server(tmp_path, configure)
    with TestClient(result.app) as client:
        assert [(s.name, s.status) for s in result.server_plugins.states()] == [
            ("users", "loaded"), ("public-api", "loaded"), ("docs", "loaded"), ("portal", "loaded"),
        ]
        # The users plugin protects the server from the first request
        assert client.get(f"{V1}/commands").status_code == 401
        assert result.builder.plugin_services.get("public-api") is not None


def test_public_api_and_portal_need_configuration_alone(tmp_path):
    from qodalis_cli_portal import plugin as portal
    from qodalis_cli_public_api import plugin as public_api

    result = server(tmp_path, lambda b: (b.add_plugin(public_api), b.add_plugin(portal)))
    with TestClient(result.app):
        assert states(result)["public-api"]["missing"] == ["the users plugin (install users, or add it before public-api)"]
        assert states(result)["portal"]["status"] == "needs-configuration"


def test_jobs_audit_docs_aws_and_the_file_storages_load_from_their_entries(tmp_path):
    from qodalis_cli_audit import plugin as audit
    from qodalis_cli_aws import plugin as aws
    from qodalis_cli_docs import plugin as docs
    from qodalis_cli_filesystem_json import plugin as fs_json
    from qodalis_cli_jobs import plugin as jobs

    def configure(b: Any) -> None:
        for entry in (jobs, audit, docs, fs_json):
            b.add_plugin(entry)
        b.add_plugin(aws, {"region": "eu-north-1"})

    result = server(tmp_path, configure)
    with TestClient(result.app) as client:
        assert {s.name: s.status for s in result.server_plugins.states()} == {
            "jobs": "loaded", "audit": "loaded", "docs": "loaded", "filesystem-json": "loaded", "aws": "loaded",
        }
        assert client.get(f"{V1}/jobs").status_code == 200
        assert os.path.isdir(tmp_path / "data" / "plugin-data" / "docs" / "pages")
        aws_processor = result.registry.find_processor("aws")
        assert aws_processor.get_config_service().get_region() == "eu-north-1"
        assert result.builder.file_storage_provider is not None
        jobs_plugin = result.builder.plugin_services.get("jobs")
    assert jobs_plugin is not None


def test_storage_entries_refuse_a_storage_already_set(tmp_path):
    from qodalis_cli_filesystem_sqlite import plugin as fs_sqlite
    from qodalis_cli_filesystem_json import plugin as fs_json

    result = server(tmp_path, lambda b: (b.add_plugin(fs_json), b.add_plugin(fs_sqlite)))
    with TestClient(result.app):
        assert states(result)["filesystem-json"]["status"] == "loaded"
        assert states(result)["filesystem-sqlite"]["error"] == "the filesystem storage is already set by the host or another plugin"


def test_databases_on_sqlite_and_config_redis_without_a_url(tmp_path):
    from qodalis_cli_config_redis import plugin as config_redis
    from qodalis_cli_databases import plugin as databases

    def configure(b: Any) -> None:
        b.add_plugin(databases, {"dialect": "sqlite", "connectionString": "main.db"})
        b.add_plugin(config_redis)

    result = server(tmp_path, configure)
    with TestClient(result.app):
        assert states(result)["databases"]["status"] == "loaded"
        assert states(result)["config-redis"]["missing"] == ["settings.url"]
        assert result.builder.plugin_services.get("databases") is not None
        assert os.path.isfile(tmp_path / "data" / "plugin-data" / "databases" / "main.db")
