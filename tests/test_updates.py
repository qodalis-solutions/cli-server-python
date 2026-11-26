"""The update checker: dashboard and server plugins installed from the marketplace with a newer version (Update),
plugins added in code (Update in code, with the install commands), the server package (PyPI), the notifications it
publishes and resolves, its schedule (a job of the jobs plugin, else a task), its persisted result,
``/api/v1/qcli/updates`` with its permissions, the ``updates_list`` MCP tool, and the jobs scheduler scheduling a job
registered after it started.  Port of the Node ``updates.test.ts``."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from qodalis_cli.notifications import CliNotificationService, CliNotificationViewer
from qodalis_cli_admin import (
    MarketplaceResponse,
    UpdateChecker,
    UpdateCheckerDeps,
    UpdateCheckOptions,
    install_commands,
    update_notification_key,
)
from qodalis_cli_jobs import CliJobScheduler, InMemoryJobStorageProvider
from qodalis_cli_server_abstractions import CliJobOptions, ICliJob

from .dashboard_marketplace_helpers import BASE, JOBS, TICKETS, FakeNet, start  # noqa: F401 (fixture)

V1 = "/api/v1/qcli"
UPDATES = f"{V1}/updates"
PYPI = "https://pypi.test"
CORE = "qodalis-cli-server"
EVERYONE = CliNotificationViewer("u", lambda _p: True)


def response(status: int, body: Any) -> MarketplaceResponse:
    return MarketplaceResponse(status, json.dumps(body).encode())


class FakeServerMarketplace:
    """A server marketplace answering ``search`` with these annotated items, and its installed list."""

    def __init__(self, items: list[dict[str, Any]], records: Optional[list[dict[str, Any]]] = None) -> None:
        self.items = items
        self.records = records or []
        self.calls: list[dict[str, Any]] = []
        self.fail: Optional[Exception] = None
        self.delay = 0.0

    async def search(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            error, self.fail = self.fail, None
            raise error
        return {"items": self.items, "total": len(self.items), "page": 1, "pageSize": 100}

    def installed_records(self) -> list[dict[str, Any]]:
        return self.records


class FakePypi:
    """PyPI's ``/pypi/<project>/json`` for the server package."""

    def __init__(self, versions: dict[str, str]) -> None:
        self.versions = versions
        self.down = False
        self.calls: list[str] = []

    async def fetch(self, url: str, headers: Any, timeout: float) -> MarketplaceResponse:
        self.calls.append(url)
        if self.down:
            raise ConnectionError("fetch failed")
        for name, version in self.versions.items():
            if url == f"{PYPI}/pypi/{quote(name, safe='')}/json":
                return response(200, {"info": {"name": name, "version": version}})
        return response(404, {"message": "Not Found"})


def checker(
    notifications: Optional[CliNotificationService] = None, options: Optional[UpdateCheckOptions] = None, **deps: Any,
) -> tuple[UpdateChecker, CliNotificationService]:
    notifications = notifications or CliNotificationService()
    deps.setdefault("server_version", lambda: "1.0.0b33")
    deps.setdefault("fetch", FakePypi({CORE: "1.0.0b33"}).fetch)
    deps.setdefault("pypi_url", PYPI)
    return UpdateChecker(UpdateCheckerDeps(notifications=lambda: notifications, **deps), options), notifications


def weather_item(installed: Optional[dict[str, str]], latest: str = "1.2.0", **extra: Any) -> dict[str, Any]:
    return {
        "name": "weather",
        "title": "Weather",
        "icon": "🌤️",
        "platforms": {
            "python": {"package": "qodalis-cli-server-plugin-weather", "latest": latest},
            "node": {"package": "@qodalis/cli-server-plugin-weather", "latest": "1.2.0"},
            "dotnet": {
                "package": "Qodalis.Cli.Plugin.Weather", "latest": "1.2.0",
                "install": "dotnet add package Qodalis.Cli.Plugin.Weather --version 1.2.0",
            },
        },
        "platform": {
            "package": "qodalis-cli-server-plugin-weather", "latest": latest,
            "installed": installed["version"] if installed else None, "compatible": True,
        },
        "installed": installed,
        "updateAvailable": installed is not None,
        "allowed": True,
        **extra,
    }


# --------------------------------------------------------------------------- the checker


async def test_server_plugins_marketplace_installs_update_here_code_added_ones_in_code_with_the_commands():
    server = FakeServerMarketplace([
        weather_item({"version": "1.0.0", "source": "marketplace"}),
        {
            **weather_item({"version": "0.9.0", "source": "code"}), "name": "jobs", "title": "Jobs", "icon": None,
            "platform": {"package": "qodalis-cli-server-plugin-jobs", "latest": "1.2.0", "installed": None, "compatible": True},
            "platforms": {
                "python": {"package": "qodalis-cli-server-plugin-jobs"},
                "node": {"package": "@qodalis/cli-server-plugin-jobs", "latest": "1.2.0"},
            },
        },
        {**weather_item(None), "name": "other"},
        {**weather_item({"version": "1.0.0", "source": "marketplace"}, "1.1.0", allowed=False, blockedReason="unverified publisher"), "name": "shady"},
    ])
    c, notifications = checker(server=server)
    result = await c.check()
    assert server.calls == [{"page": 1, "page_size": 100, "platform": "this"}]
    assert result["serverPlugins"][0] == {
        "kind": "server", "name": "weather", "package": "qodalis-cli-server-plugin-weather", "title": "Weather", "icon": "🌤️",
        "current": "1.0.0", "latest": "1.2.0", "source": "marketplace", "updateAvailable": True, "canUpdate": True,
    }
    shady = result["serverPlugins"][1]
    assert (shady["name"], shady["canUpdate"], shady["reason"], shady["latest"]) == ("shady", False, "unverified publisher", "1.1.0")
    [jobs] = result["codePlugins"]
    assert {k: jobs[k] for k in ("kind", "name", "source", "current", "latest", "canUpdate", "reason", "install")} == {
        "kind": "server", "name": "jobs", "source": "code", "current": "0.9.0", "latest": "1.2.0", "canUpdate": False,
        "reason": "update in code",
        "install": {
            "python": "pip install --upgrade qodalis-cli-server-plugin-jobs==1.2.0",
            "node": "npm install @qodalis/cli-server-plugin-jobs@1.2.0",
        },
    }
    assert result["count"] == 3
    assert {k: result["server"][k] for k in ("kind", "current", "latest", "updateAvailable", "source")} == {
        "kind": "core", "current": "1.0.0b33", "latest": "1.0.0b33", "updateAvailable": False, "source": "pypi",
    }
    assert result["checkedAt"].endswith("Z") and result["checking"] is False and "error" not in result

    keys = sorted(n["key"] for n in notifications.list(EVERYONE)["items"])
    assert keys == ["update:code:server:qodalis-cli-server-plugin-jobs", "update:server:shady", "update:server:weather"]
    weather = notifications.get("update:server:weather")
    assert (weather["kind"], weather["severity"], weather["title"], weather["link"], weather["requiredPermission"]) == (
        "update", "info", "Update available: Weather 1.2.0", "/plugins?tab=updates", "updates:read",
    )
    assert weather["message"] == "qodalis-cli-server-plugin-weather 1.0.0 → 1.2.0."
    assert weather["data"] == {
        "kind": "server", "name": "weather", "package": "qodalis-cli-server-plugin-weather", "current": "1.0.0",
        "latest": "1.2.0", "source": "marketplace", "canUpdate": True,
    }
    assert notifications.get("update:server:shady")["message"] == "qodalis-cli-server-plugin-weather 1.0.0 → 1.1.0. unverified publisher"
    code = notifications.get("update:code:server:qodalis-cli-server-plugin-jobs")
    assert code["title"] == "Update in code: Jobs 1.2.0"
    assert code["message"] == (
        "qodalis-cli-server-plugin-jobs 0.9.0 → 1.2.0. The host adds it in code: "
        "pip install --upgrade qodalis-cli-server-plugin-jobs==1.2.0, then restart."
    )


def test_install_commands_this_platform_first_then_the_others():
    assert install_commands([
        {"platform": "node", "package": "@acme/x", "latest": "2.0.0"},
        {"platform": "dotnet", "package": "Acme.X", "latest": "2.0.0"},
        {"platform": "dotnet", "package": "Acme.Y", "install": "dotnet add package Acme.Y"},
        {"platform": "go"},
    ], "acme-x", "2.0.0b1") == {
        "python": "pip install --upgrade acme-x==2.0.0b1",
        "node": "npm install @acme/x@2.0.0",
        "dotnet": "dotnet add package Acme.Y",
    }
    assert install_commands(None, "acme-x", None) == {"python": "pip install --upgrade acme-x"}


async def test_the_server_package_newer_on_pypi_is_an_update_and_a_notification():
    c, notifications = checker(fetch=FakePypi({CORE: "1.0.0b40"}).fetch)
    result = await c.check()
    assert {k: result["server"][k] for k in ("current", "latest", "updateAvailable", "canUpdate", "install", "reason")} == {
        "current": "1.0.0b33", "latest": "1.0.0b40", "updateAvailable": True, "canUpdate": False,
        "install": {"python": "pip install --upgrade qodalis-cli-server==1.0.0b40"},
        "reason": "update the server packages in code (every qodalis-cli-server* package shares the version)",
    }
    assert result["count"] == 1
    n = notifications.get(f"update:core:{CORE}")
    assert n["title"] == "Server update available: 1.0.0b40"
    assert n["message"] == "qodalis-cli-server 1.0.0b33 → 1.0.0b40. Update the server packages in code and restart."


async def test_prereleases_compare_as_pep_440():
    c, _ = checker(fetch=FakePypi({CORE: "1.0.0"}).fetch)
    assert (await c.check())["server"]["updateAvailable"] is True
    c, _ = checker(fetch=FakePypi({CORE: "1.0.0b9"}).fetch, server_version=lambda: "1.0.0b10")
    assert (await c.check())["server"]["updateAvailable"] is False


async def test_resolves_the_notifications_of_updates_that_no_longer_apply():
    server = FakeServerMarketplace([weather_item({"version": "1.0.0", "source": "marketplace"})])
    c, notifications = checker(server=server)
    await c.check()
    assert notifications.get("update:server:weather") is not None
    server.items = [{**weather_item({"version": "1.2.0", "source": "marketplace"}), "updateAvailable": False}]
    result = await c.check()
    assert result["serverPlugins"] == []
    assert notifications.get("update:server:weather") is None


async def test_a_marketplace_that_does_not_answer_keeps_the_last_items_and_their_notifications():
    server = FakeServerMarketplace([weather_item({"version": "1.0.0", "source": "marketplace"})])
    c, notifications = checker(server=server)
    await c.check()
    server.fail = RuntimeError("marketplace unreachable")
    result = await c.check()
    assert result["error"] == "server plugins: marketplace unreachable"
    assert [i["name"] for i in result["serverPlugins"]] == ["weather"]
    assert notifications.get("update:server:weather") is not None


async def test_the_package_index_down_says_so_and_keeps_the_last_server_item():
    pypi = FakePypi({CORE: "1.0.0b40"})
    c, notifications = checker(fetch=pypi.fetch)
    await c.check()
    pypi.down = True
    result = await c.check()
    assert result["error"] == "server package: package index unreachable"
    assert (result["server"]["latest"], result["server"]["updateAvailable"]) == ("1.0.0b40", True)
    assert notifications.get(f"update:core:{CORE}") is not None


async def test_several_failures_are_listed_together():
    pypi = FakePypi({})
    pypi.down = True
    server = FakeServerMarketplace([])
    server.fail = RuntimeError("marketplace unreachable")
    c, _ = checker(fetch=pypi.fetch, server=server)
    result = await c.check()
    assert result["error"] == "server plugins: marketplace unreachable; server package: package index unreachable"
    assert result["server"] is None


async def test_joins_a_check_already_running():
    server = FakeServerMarketplace([])
    server.delay = 0.05
    c, _ = checker(server=server)
    task = asyncio.ensure_future(c.check())
    await asyncio.sleep(0.01)
    assert c.result()["checking"] is True
    a, b = await asyncio.gather(task, c.check())
    assert len(server.calls) == 1
    assert a == b and a["checking"] is False


async def test_keeps_the_last_result_across_a_restart(tmp_path):
    file = str(tmp_path / "data" / "updates.json")
    server = FakeServerMarketplace([weather_item({"version": "1.0.0", "source": "marketplace"})])
    first, _ = checker(server=server, file=file, fetch=FakePypi({CORE: "1.0.0b40"}).fetch)
    await first.check()
    with open(file, encoding="utf-8") as f:
        assert "checking" not in json.load(f)
    again = checker(server=server, file=file)[0].result()
    assert (again["count"], again["checking"], [i["name"] for i in again["serverPlugins"]]) == (2, False, ["weather"])
    assert (again["server"]["latest"], again["server"]["updateAvailable"]) == ("1.0.0b40", True)
    # the server was updated in code since: its running version is read again
    updated = checker(server=server, file=file, server_version=lambda: "1.0.0b40")[0].result()
    assert (updated["count"], updated["server"]["current"], updated["server"]["updateAvailable"]) == (1, "1.0.0b40", False)


def test_before_the_first_check():
    c, _ = checker()
    assert c.result() == {
        "checkedAt": None, "checking": False, "server": None, "dashboardPlugins": [], "serverPlugins": [],
        "codePlugins": [], "count": 0,
    }


async def test_refresh_installed_drops_updated_or_removed_marketplace_plugins_without_asking_the_marketplace():
    records = [{"name": "weather", "version": "1.0.0"}]
    server = FakeServerMarketplace([weather_item({"version": "1.0.0", "source": "marketplace"})], records)
    c, notifications = checker(server=server)
    await c.check()
    c.refresh_installed()
    assert len(c.result()["serverPlugins"]) == 1
    records[0]["version"] = "1.2.0"
    c.refresh_installed()
    assert (c.result()["serverPlugins"], c.result()["count"]) == ([], 0)
    assert notifications.get("update:server:weather") is None
    assert len(server.calls) == 1


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, Any] = {}
        self.registered: list[tuple[ICliJob, CliJobOptions]] = []

    def register(self, job: ICliJob, options: CliJobOptions) -> str:
        self.registered.append((job, options))
        self.jobs[options.id] = job
        return options.id


class _Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, text: str) -> None:
        self.lines.append(text)

    debug = warning = error = info


async def test_schedules_a_job_of_the_jobs_plugin_when_one_is_mounted_the_first_check_after_the_initial_delay():
    server = FakeServerMarketplace([])
    scheduler = FakeScheduler()
    c, _ = checker(server=server, scheduler=lambda: scheduler, options=UpdateCheckOptions(initial_delay=0.05))
    c.start()
    c.start()  # once
    [(job, options)] = scheduler.registered
    assert (options.id, options.name, options.group, options.tags, options.interval) == (
        "updates:check", "updates:check", "system", ["system", "updates"], "21600s",
    )
    assert server.calls == []
    await asyncio.sleep(0.15)
    assert len(server.calls) == 1
    # the job runs the check
    logger = type("Ctx", (), {"logger": _Logger()})()
    await job.execute_async(logger, asyncio.Event())
    assert len(server.calls) == 2
    assert logger.logger.lines == ["0 update(s) available"]
    await c.stop()


async def test_a_failing_check_fails_the_job():
    pypi = FakePypi({})
    pypi.down = True
    scheduler = FakeScheduler()
    c, _ = checker(fetch=pypi.fetch, scheduler=lambda: scheduler, options=UpdateCheckOptions(initial_delay=60))
    c.start()
    [(job, _options)] = scheduler.registered
    with pytest.raises(RuntimeError, match="package index unreachable"):
        await job.execute_async(type("Ctx", (), {"logger": _Logger()})(), asyncio.Event())
    await c.stop()


async def test_without_the_jobs_plugin_a_task_checks_every_interval():
    server = FakeServerMarketplace([])
    c, _ = checker(server=server, options=UpdateCheckOptions(initial_delay=0.02, interval=0.1))
    c.start()
    await asyncio.sleep(0.06)
    assert len(server.calls) == 1
    await asyncio.sleep(0.12)
    assert len(server.calls) == 2
    await c.stop()
    await asyncio.sleep(0.15)
    assert len(server.calls) == 2


async def test_a_fresh_last_result_waits_until_it_is_due(tmp_path):
    file = str(tmp_path / "updates.json")
    server = FakeServerMarketplace([])
    await checker(server=server, file=file)[0].check()
    c, _ = checker(server=server, file=file, options=UpdateCheckOptions(initial_delay=0.01, interval=0.3))
    c.start()
    await asyncio.sleep(0.1)
    assert len(server.calls) == 1  # the one before: checked less than an interval ago
    await asyncio.sleep(0.3)
    assert len(server.calls) >= 2
    await c.stop()


async def test_periodic_checks_off():
    server = FakeServerMarketplace([])
    scheduler = FakeScheduler()
    c, _ = checker(server=server, scheduler=lambda: scheduler, options=UpdateCheckOptions(enabled=False, initial_delay=0.01))
    c.start()
    await asyncio.sleep(0.05)
    assert server.calls == [] and scheduler.registered == []
    await c.check()  # Check now still works
    assert len(server.calls) == 1


def test_update_notification_key():
    assert update_notification_key({"kind": "dashboard", "name": TICKETS, "package": TICKETS, "source": "marketplace"}) == (
        f"update:dashboard:{TICKETS}"
    )
    assert update_notification_key({"kind": "server", "name": "weather", "package": "p", "source": "marketplace"}) == (
        "update:server:weather"
    )
    assert update_notification_key({"kind": "dashboard", "name": JOBS, "package": JOBS, "source": "code"}) == (
        f"update:code:dashboard:{JOBS}"
    )
    assert update_notification_key({"kind": "core", "name": CORE, "package": CORE, "source": "pypi"}) == f"update:core:{CORE}"


# --------------------------------------------------------------------------- the API


def test_finds_a_newer_installed_dashboard_plugin_and_a_page_added_in_code_update_clears_it(start):  # noqa: F811
    net = FakeNet()
    net.publish(TICKETS, [{"version": "1.0.0"}], title="Tickets")
    net.publish(JOBS, [{"version": "1.0.0"}, {"version": "1.3.0"}], title="Jobs")
    h = start(net=net, configure=lambda r: r.builder.add_dashboard_extension(
        {"id": "jobs", "title": "Jobs", "entry": "/qcli/modules/jobs/extension.js", "package": JOBS, "version": "1.0.0"},
    ))
    assert h.post({"name": TICKETS}).status_code == 201

    # before any check
    before = h.get(UPDATES)
    assert before.status_code == 200 and before.headers["cache-control"] == "no-store"
    assert {k: before.json()[k] for k in ("checkedAt", "count", "dashboardPlugins")} == {
        "checkedAt": None, "count": 0, "dashboardPlugins": [],
    }

    net.publish(TICKETS, [{"version": "1.0.0"}, {"version": "1.1.0"}], title="Tickets")
    res = h.client.post(f"{UPDATES}/check", headers=h.auth())
    assert res.status_code == 200, res.text
    checked = res.json()
    [tickets] = checked["dashboardPlugins"]
    assert {k: tickets[k] for k in ("kind", "name", "package", "title", "current", "latest", "source", "canUpdate")} == {
        "kind": "dashboard", "name": TICKETS, "package": TICKETS, "title": "Tickets", "current": "1.0.0", "latest": "1.1.0",
        "source": "marketplace", "canUpdate": True,
    }
    [jobs] = checked["codePlugins"]
    assert {k: jobs[k] for k in ("kind", "name", "current", "latest", "source", "canUpdate", "install")} == {
        "kind": "dashboard", "name": JOBS, "current": "1.0.0", "latest": "1.3.0", "source": "code", "canUpdate": False,
        "install": {"node": f"npm install {JOBS}@1.3.0"},
    }
    assert checked["count"] == 2 and "error" not in checked
    assert {k: h.get(UPDATES).json()[k] for k in ("count", "checking")} == {"count": 2, "checking": False}

    keys = [n["key"] for n in h.client.get(f"{V1}/notifications").json()["items"]]
    assert f"update:dashboard:{TICKETS}" in keys and f"update:code:dashboard:{JOBS}" in keys

    # updated from the dashboard: gone from the list, its notification resolved
    assert h.put(TICKETS, {}).status_code == 200
    after = h.get(UPDATES).json()
    assert (after["count"], after["dashboardPlugins"]) == (1, [])
    assert h.result.notifications.get(f"update:dashboard:{TICKETS}") is None


def test_the_marketplace_down_keeps_the_last_items_and_says_why(start):  # noqa: F811
    net = FakeNet()
    net.publish(TICKETS, [{"version": "1.0.0"}])
    h = start(net=net)
    assert h.post({"name": TICKETS}).status_code == 201
    net.publish(TICKETS, [{"version": "1.0.0"}, {"version": "1.1.0"}])
    assert h.client.post(f"{UPDATES}/check", headers=h.auth()).json()["count"] == 1
    net.marketplace_down = True
    res = h.client.post(f"{UPDATES}/check", headers=h.auth()).json()
    assert res["error"] == "dashboard plugins: marketplace unreachable; server plugins: marketplace unreachable"
    assert [i["name"] for i in res["dashboardPlugins"]] == [TICKETS]


def test_needs_the_admin_sign_in(start):  # noqa: F811
    h = start()
    assert h.client.get(UPDATES).status_code == 401
    assert h.client.post(f"{UPDATES}/check").status_code == 401


def test_protected_server_updates_read_or_plugins_install(start):  # noqa: F811
    h = start(users={
        "viewer": ["admin:access"],
        "reader": ["admin:access", "updates:read"],
        "installer": ["admin:access", "plugins:install"],
    })
    assert h.get(UPDATES, "viewer").status_code == 403
    assert h.get(UPDATES, "viewer").json() == {"error": "forbidden: missing permission updates:read"}
    assert h.client.post(f"{UPDATES}/check", headers=h.auth("viewer")).status_code == 403
    assert h.get(UPDATES, "reader").status_code == 200
    assert h.get(UPDATES, "installer").status_code == 200
    assert h.client.post(f"{UPDATES}/check", headers=h.auth("reader")).status_code == 200
    # the update notifications reach only those who may read the updates
    h.result.notifications.notify({
        "key": "update:server:x", "kind": "update", "severity": "info", "title": "Update available: X 2.0.0",
        "message": "", "requiredPermission": "updates:read",
    })

    def kinds(who: str) -> list[str]:
        return [n["kind"] for n in h.get(f"{V1}/notifications", who).json()["items"]]

    assert "update" not in kinds("viewer")
    assert "update" in kinds("reader")


def test_no_updates_without_the_marketplace(tmp_path):
    from qodalis_cli import CliServerOptions, create_cli_server
    from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder

    result = create_cli_server(CliServerOptions(data_dir=str(tmp_path)))
    admin = CliAdminBuilder().set_credentials("admin", "pass").set_marketplace(False).build(AdminBuildDeps(
        registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder,
    ))
    admin.log_buffer.uninstall_handler()
    assert admin.updates is None and admin.mcp_providers == []


def test_the_updates_list_mcp_tool(tmp_path):
    from qodalis_cli import CliServerOptions, create_cli_server
    from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder, DashboardMarketplaceOptions

    net = FakeNet()
    result = create_cli_server(CliServerOptions(data_dir=str(tmp_path), mcp=True))
    admin = (
        CliAdminBuilder().set_credentials("admin", "pass")
        .set_marketplace(DashboardMarketplaceOptions(url="https://market.test", fetch=net.fetch))
        .set_update_checks(False)
        .build(AdminBuildDeps(registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder))
    )
    admin.log_buffer.uninstall_handler()
    result.mount_plugin(admin)
    with TestClient(result.app) as client:
        def call(args: dict[str, Any]) -> dict[str, Any]:
            res = client.post("/api/qcli/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "updates_list", "arguments": args},
            })
            assert res.status_code == 200, res.text
            return res.json()["result"]

        last = call({})
        assert last["isError"] is False
        assert last["structuredContent"]["checkedAt"] is None
        assert last["content"][0]["text"] == "0 update(s) available; last checked never"
        checked = call({"check": True})
        assert checked["structuredContent"]["checkedAt"] is not None


# --------------------------------------------------------------------------- the update check under the app


def test_the_check_starts_with_the_app_through_the_jobs_plugin_and_stops_with_it(tmp_path):
    from qodalis_cli import CliServerOptions, create_cli_server
    from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder, DashboardMarketplaceOptions
    from qodalis_cli_jobs import CliJobsBuilder

    net = FakeNet()
    result = create_cli_server(CliServerOptions(data_dir=str(tmp_path)))
    jobs = CliJobsBuilder().build()
    result.mount_plugin(jobs)
    assert result.builder.plugin_services.get("jobs.scheduler") is jobs.scheduler
    admin = (
        CliAdminBuilder().set_credentials("admin", "pass")
        .set_marketplace(DashboardMarketplaceOptions(url="https://market.test", fetch=net.fetch))
        .set_update_checks({"initial_delay": 3600})
        .build(AdminBuildDeps(registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder))
    )
    admin.log_buffer.uninstall_handler()
    result.mount_plugin(admin)
    with TestClient(result.app):
        registration = jobs.scheduler.jobs["updates:check"]
        assert (registration.options.group, registration.options.interval) == ("system", "21600s")
        assert admin.updates._tasks
    assert admin.updates._tasks == []


# --------------------------------------------------------------------------- the jobs scheduler


async def test_a_job_registered_after_the_scheduler_started_is_scheduled_at_once():
    scheduler = CliJobScheduler(InMemoryJobStorageProvider())
    await scheduler.start()
    runs: list[int] = []

    class Late(ICliJob):
        async def execute_async(self, context: Any, cancellation_event: asyncio.Event) -> None:
            runs.append(1)

    try:
        scheduler.register(Late(), CliJobOptions(id="late", name="late", interval="1s"))
        assert scheduler.jobs["late"].next_run_at is not None
        await asyncio.sleep(1.5)
        assert len(runs) == 1
    finally:
        await scheduler.stop()
