"""Installed server plugins: the builder's list and the admin endpoint."""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qodalis_cli import CliServerOptions, create_cli_server  # noqa: E402
from qodalis_cli.extensions import CliBuilder  # noqa: E402
from qodalis_cli.models.cli_installed_plugin import (  # noqa: E402
    CliInstalledPlugin,
    distribution_of,
    short_plugin_name,
)
from qodalis_cli.services import CliCommandRegistry  # noqa: E402
from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder  # noqa: E402
from qodalis_cli_filesystem.providers import InMemoryFileStorageProvider  # noqa: E402
from qodalis_cli_jobs import CliJobsBuilder  # noqa: E402
from qodalis_cli_server_abstractions.jobs import CliJobOptions  # noqa: E402
from qodalis_cli_users import CliUsersBuilder  # noqa: E402
from qodalis_cli_weather import WeatherModule  # noqa: E402
from tests.test_admin_plugin import _StubEventSocketManager, _StubRegistry  # noqa: E402
from tests.test_jobs import DummyJob  # noqa: E402

CORE = "qodalis-cli-server"
USERS = "qodalis-cli-server-plugin-users"
ADMIN = "qodalis-cli-server-plugin-admin"
JOBS = "qodalis-cli-server-plugin-jobs"
AUDIT = "qodalis-cli-server-plugin-audit"


class _HostPlugin:
    """A plugin object of the host's own code (no distribution)."""


def _ids(builder: CliBuilder) -> list[str]:
    return [p.id for p in builder.installed_plugins]


class TestCliBuilderInstalledPlugins:
    def test_core_is_listed_first(self) -> None:
        builder = CliBuilder(CliCommandRegistry())
        assert _ids(builder) == [CORE]
        assert builder.installed_plugins[0].name == "Core"

    def test_modules_and_providers_list_their_packages_once(self) -> None:
        builder = CliBuilder(CliCommandRegistry())
        builder.add_module(WeatherModule())
        builder.add_module(WeatherModule())
        builder.set_file_storage_provider(InMemoryFileStorageProvider())

        assert _ids(builder) == [
            CORE,
            "qodalis-cli-server-plugin-weather",
            "qodalis-cli-server-plugin-filesystem",
        ]

    def test_host_code_is_not_listed_but_a_named_package_is(self) -> None:
        builder = CliBuilder(CliCommandRegistry())
        builder.add_installed_plugin(_HostPlugin())
        builder.add_installed_plugin("acme-billing", name="Billing", description="Billing commands")

        assert _ids(builder) == [CORE, "acme-billing"]
        billing = builder.installed_plugins[1]
        assert (billing.name, billing.version, billing.description) == (
            "Billing", None, "Billing commands",
        )

    def test_mounted_plugins_are_listed_in_order(self) -> None:
        result = create_cli_server(CliServerOptions())
        users = CliUsersBuilder().set_default_admin_password("rootpass").build()
        jobs = CliJobsBuilder().add_job(DummyJob(), CliJobOptions(name="a")).build()
        admin = CliAdminBuilder().set_credentials("a", "b").set_jwt_secret("x" * 32).build(
            AdminBuildDeps(
                registry=result.registry,
                event_socket_manager=result.event_socket_manager,
                builder=result.builder,
            )
        )

        result.mount_plugin(admin)
        result.mount_plugin(users)
        result.mount_plugin(jobs)
        result.mount_plugin(_HostPlugin())
        result.mount_plugin(jobs)

        assert _ids(result.builder) == [CORE, ADMIN, USERS, AUDIT, JOBS]
        plugin = result.builder.installed_plugins[2]
        assert plugin.name == "Users"
        assert plugin.version
        assert "User management" in (plugin.description or "")


class TestInstalledPluginMetadata:
    def test_short_names(self) -> None:
        assert short_plugin_name(CORE) == "Core"
        assert short_plugin_name(USERS) == "Users"
        assert short_plugin_name("qodalis-cli-server-plugin-data-explorer-sql") == "Data Explorer Sql"
        assert short_plugin_name("acme-billing") == "acme-billing"

    def test_distribution_of_a_qodalis_object(self) -> None:
        assert distribution_of(WeatherModule()) == "qodalis-cli-server-plugin-weather"
        assert distribution_of(CliBuilder) == CORE
        assert distribution_of(_HostPlugin) is None

    def test_a_package_that_is_not_installed_has_no_version(self) -> None:
        plugin = CliInstalledPlugin.from_distribution("qodalis-cli-server-plugin-not-installed")
        assert plugin.to_dict() == {
            "id": "qodalis-cli-server-plugin-not-installed",
            "name": "Not Installed",
            "version": None,
            "description": None,
        }


# ---------------------------------------------------------------------------
# Admin: GET /plugins/installed and the status's enabled features
# ---------------------------------------------------------------------------

@pytest.fixture()
def builder() -> CliBuilder:
    builder = CliBuilder(CliCommandRegistry())
    builder.set_file_storage_provider(InMemoryFileStorageProvider())
    return builder


@pytest.fixture()
async def client(builder: CliBuilder):
    admin = CliAdminBuilder().set_credentials("admin", "pw").set_jwt_secret("s" * 32).build(
        AdminBuildDeps(
            registry=_StubRegistry(),
            event_socket_manager=_StubEventSocketManager(),
            builder=builder,
        )
    )
    builder.add_installed_plugin(ADMIN)
    app = FastAPI()
    app.include_router(admin.router, prefix="/api/v1/qcli")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.post("/api/v1/qcli/auth/login", json={"username": "admin", "password": "pw"})
        c.headers["Authorization"] = f"Bearer {res.json()['token']}"
        yield c


class TestAdminInstalledPlugins:
    async def test_lists_the_installed_packages(self, client: AsyncClient, builder: CliBuilder) -> None:
        builder.add_installed_plugin(USERS)

        res = await client.get("/api/v1/qcli/plugins/installed")

        assert res.status_code == 200
        body = res.json()
        assert [p["id"] for p in body] == [
            CORE, "qodalis-cli-server-plugin-filesystem", ADMIN, USERS,
        ]
        assert body[0]["name"] == "Core"
        assert set(body[3]) == {"id", "name", "version", "description"}

    async def test_requires_authentication(self, client: AsyncClient) -> None:
        del client.headers["Authorization"]
        assert (await client.get("/api/v1/qcli/plugins/installed")).status_code == 401

    async def test_status_reports_audit_when_the_audit_plugin_is_installed(
        self, client: AsyncClient, builder: CliBuilder,
    ) -> None:
        res = await client.get("/api/v1/qcli/status")
        assert res.json()["enabledFeatures"] == ["filesystem"]

        # The users plugin alone no longer reports it: the audit plugin serves the log.
        builder.add_installed_plugin(USERS)
        builder.add_installed_plugin(JOBS)
        res = await client.get("/api/v1/qcli/status")
        assert res.json()["enabledFeatures"] == ["filesystem", "jobs"]

        builder.add_installed_plugin(AUDIT)
        res = await client.get("/api/v1/qcli/status")
        assert res.json()["enabledFeatures"] == ["filesystem", "jobs", "audit"]
