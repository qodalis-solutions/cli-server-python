"""Dashboard module pages: the helper (``qodalis_cli_server_abstractions.dashboard_modules``), the ``widgets`` and
``search`` fields of an extension, and the pages of the plugins that own their APIs (jobs, audit, API tokens,
filesystem, data explorer), served to anyone at ``/qcli/modules/<id>`` while their APIs stay protected."""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qodalis_cli import CliServerOptions, create_cli_server  # noqa: E402
from qodalis_cli.dashboard_modules import (  # noqa: E402
    DATA_EXPLORER_PAGE,
    FILESYSTEM_PAGE,
    dashboard_module_page,
    find_dashboard_module,
    module_page_of,
)
from qodalis_cli.extensions import CliBuilder  # noqa: E402
from qodalis_cli.filesystem import FileSystemOptions  # noqa: E402
from qodalis_cli.models import CliDashboardExtension  # noqa: E402
from qodalis_cli.services import CliCommandRegistry  # noqa: E402
from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder  # noqa: E402
from qodalis_cli_audit import CliAuditBuilder  # noqa: E402
from qodalis_cli_audit.cli_audit_builder import AUDIT_DASHBOARD_PAGE  # noqa: E402
from qodalis_cli_jobs import CliJobsBuilder  # noqa: E402
from qodalis_cli_jobs.cli_jobs_builder import JOBS_DASHBOARD_PAGE  # noqa: E402
from qodalis_cli_server_abstractions import (  # noqa: E402
    DataExplorerExecutionContext,
    DataExplorerProviderOptions,
    DataExplorerResult,
    IDataExplorerProvider,
)
from qodalis_cli_users import CliUsersBuilder  # noqa: E402
from qodalis_cli_users.cli_users_builder import TOKENS_DASHBOARD_PAGE  # noqa: E402
from tests.test_admin_plugin import _StubEventSocketManager, _StubRegistry  # noqa: E402

ROOT_PASS = "rootpass1"
HASHED = r"\?v=[0-9a-f]{12}"


def _build(directory, css: bool = True):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "extension.js").write_text("export default {};")
    if css:
        (directory / "extension.css").write_text(".x{}")
    return str(directory)


class _Provider(IDataExplorerProvider):
    async def execute_async(self, context: DataExplorerExecutionContext) -> DataExplorerResult:  # pragma: no cover
        raise NotImplementedError

    async def get_schema_async(self, options: DataExplorerProviderOptions) -> Any:  # pragma: no cover
        return None


class TestHelper:
    def test_serves_the_build_without_caching_and_points_the_extension_at_it(self, tmp_path) -> None:
        page = dashboard_module_page(_build(tmp_path / "b"), "/qcli/modules/x/", {"id": "x", "title": "X", "widgets": []})
        assert page.prefix == "/qcli/modules/x"
        assert re.fullmatch(rf"/qcli/modules/x/extension\.js{HASHED}", page.extension["entry"])
        assert re.fullmatch(rf"/qcli/modules/x/extension\.css{HASHED}", page.extension["styles"][0])
        assert "widgets" not in page.extension
        app = FastAPI()
        app.mount(page.prefix, page.app)
        res = TestClient(app).get("/qcli/modules/x/extension.js")
        assert (res.status_code, res.text, res.headers["cache-control"]) == (200, "export default {};", "no-cache")

    def test_leaves_styles_out_without_a_stylesheet(self, tmp_path) -> None:
        page = dashboard_module_page(_build(tmp_path / "b", css=False), "/p", {"id": "x", "title": "X"})
        assert "styles" not in page.extension

    def test_a_new_build_gets_a_new_url(self, tmp_path) -> None:
        directory = _build(tmp_path / "b")
        first = dashboard_module_page(directory, "/p", {"id": "x", "title": "X"}).extension["entry"]
        (tmp_path / "b" / "extension.js").write_text("export default { v: 2 };")
        assert dashboard_module_page(directory, "/p", {"id": "x", "title": "X"}).extension["entry"] != first

    def test_finds_the_copy_in_the_package_or_a_given_directory(self, tmp_path) -> None:
        assert find_dashboard_module(str(tmp_path)) is None
        _build(tmp_path / "dashboard")
        assert find_dashboard_module(str(tmp_path)) == str(tmp_path / "dashboard")
        assert find_dashboard_module(str(tmp_path), str(tmp_path / "other")) is None

    def test_module_page_of_leaves_it_out_or_warns_without_a_build(self, tmp_path, caplog) -> None:
        _build(tmp_path / "dashboard")
        assert module_page_of(str(tmp_path), False, "/p", {"id": "x", "title": "X"}) is None
        assert module_page_of(str(tmp_path), None, "/p", {"id": "x", "title": "X"}) is not None
        with caplog.at_level(logging.WARNING):
            assert module_page_of(str(tmp_path / "none"), None, "/p", {"id": "x", "title": "X"}) is None
        assert "the dashboard's x page was not found" in caplog.text


class TestExtensionFields:
    def test_widgets_and_search_are_validated_and_serialized_only_when_set(self) -> None:
        ext = CliDashboardExtension.of(
            {"id": "jobs", "title": "Jobs", "entry": "/j.js", "widgets": ["overview", "palette", "overview"], "search": "/?q="}
        ).validated()
        assert ext.to_dict() == {"id": "jobs", "title": "Jobs", "entry": "/j.js", "widgets": ["overview", "palette"], "search": "/?q="}
        assert "widgets" not in CliDashboardExtension(id="a", title="A", entry="/a.js", widgets=()).validated().to_dict()
        for bad in ({"widgets": ["Over view"]}, {"widgets": ["a", "b", "c", "d", "e", "f"]}, {"search": "?q="}, {"search": "/ q"}):
            with pytest.raises(ValueError, match="invalid dashboard extension"):
                CliDashboardExtension.of({"id": "a", "title": "A", "entry": "/a.js", **bad}).validated()

    async def test_the_extensions_endpoint_returns_them(self) -> None:
        builder = CliBuilder(CliCommandRegistry())
        builder.add_dashboard_extension({"id": "jobs", "title": "Jobs", "entry": "/j.js", "widgets": ["overview"], "search": "/?q="})
        admin = CliAdminBuilder().set_credentials("admin", "pw").set_jwt_secret("s" * 32).build(
            AdminBuildDeps(registry=_StubRegistry(), event_socket_manager=_StubEventSocketManager(), builder=builder)
        )
        app = FastAPI()
        app.include_router(admin.router, prefix="/api/v1/qcli")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            token = (await c.post("/api/v1/qcli/auth/login", json={"username": "admin", "password": "pw"})).json()["token"]
            res = await c.get("/api/v1/qcli/dashboard/extensions", headers={"Authorization": f"Bearer {token}"})
        assert res.json()["extensions"] == [
            {"id": "jobs", "title": "Jobs", "entry": "/j.js", "widgets": ["overview"], "search": "/?q="}
        ]


CONTRACT = {
    "jobs": ("Jobs", "Joburi", "⏱", "operations", 10, "jobs:read", ("overview", "palette"), "/?q="),
    "audit": ("Audit", "Audit", "🛡", "operations", 30, "audit:read", None, "/?q="),
    "tokens": ("API tokens", "Tokenuri API", "🔑", "access", 10, None, None, None),
    "filesystem": ("Filesystem", "Fișiere", "📁", "data", 10, "files:read", None, None),
    "data-explorer": ("Data explorer", "Explorator de date", "⛁", "data", 15, "data:query", None, None),
}


class TestPluginPages:
    def _server(self, tmp_path):
        builds = {k: _build(tmp_path / k) for k in CONTRACT}

        def configure(builder: CliBuilder) -> None:
            builder.add_filesystem(FileSystemOptions(allowed_paths=[str(tmp_path)]))
            builder.add_data_explorer_provider(_Provider(), DataExplorerProviderOptions(name="db", description="db"))

        result = create_cli_server(CliServerOptions(
            configure=configure,
            dashboard_pages={"filesystem": builds["filesystem"], "data-explorer": builds["data-explorer"]},
        ))
        users = (
            CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000)
            .set_dashboard_dir(builds["tokens"]).build()
        )
        result.mount_plugin(CliAuditBuilder().set_dashboard_dir(builds["audit"]).build())
        result.mount_plugin(users)
        result.mount_plugin(CliJobsBuilder().set_dashboard_dir(builds["jobs"]).build())
        return result

    def test_each_page_is_registered_as_the_contract_says(self, tmp_path) -> None:
        result = self._server(tmp_path)
        extensions = {e.id: e for e in result.builder.dashboard_extensions}
        assert set(extensions) == set(CONTRACT)
        for ext_id, (title, ro, icon, group, order, permission, widgets, search) in CONTRACT.items():
            ext = extensions[ext_id]
            assert (ext.title, ext.titles["ro"], ext.icon, ext.group, ext.order, ext.permission, ext.widgets, ext.search) == (
                title, ro, icon, group, order, permission, widgets, search,
            ), ext_id
            assert len(ext.titles) == 10
            assert re.fullmatch(rf"/qcli/modules/{ext_id}/extension\.js{HASHED}", ext.entry)
            assert re.fullmatch(rf"/qcli/modules/{ext_id}/extension\.css{HASHED}", ext.styles[0])
            assert f"/qcli/modules/{ext_id}" in result.cli_route_prefixes

    def test_pages_are_served_to_anyone_while_their_apis_stay_protected(self, tmp_path) -> None:
        result = self._server(tmp_path)
        with TestClient(result.app) as c:
            for ext_id in CONTRACT:
                res = c.get(f"/qcli/modules/{ext_id}/extension.js")
                assert (res.status_code, res.text) == (200, "export default {};"), ext_id
                assert res.headers["cache-control"] == "no-cache"
            for api in ("/api/v1/qcli/jobs", "/api/v1/qcli/audit", "/api/v1/qcli/tokens", "/api/qcli/fs/ls?path=/"):
                assert c.get(api).status_code == 401, api

    def test_the_audit_page_is_served_once_when_two_audit_plugins_are_mounted(self, tmp_path) -> None:
        result = create_cli_server(CliServerOptions())
        result.mount_plugin(CliAuditBuilder().set_dashboard_dir(_build(tmp_path / "a")).build())
        result.mount_plugin(CliAuditBuilder().set_dashboard_dir(_build(tmp_path / "b")).build())
        assert [e.id for e in result.builder.dashboard_extensions] == ["audit"]

    def test_pages_can_be_left_out(self, tmp_path) -> None:
        assert CliJobsBuilder().set_dashboard_dir(False).build().dashboard_pages == []
        assert CliAuditBuilder().set_dashboard_dir(False).build().dashboard_pages == []
        assert CliUsersBuilder().set_dashboard_dir(False).build().dashboard_pages == []
        assert CliUsersBuilder().set_dashboard_dir(_build(tmp_path / "t")).set_guard_only().build().dashboard_pages == []

        def configure(builder: CliBuilder) -> None:
            builder.add_filesystem(FileSystemOptions(allowed_paths=[str(tmp_path)]))

        result = create_cli_server(CliServerOptions(configure=configure, dashboard_pages={"filesystem": False}))
        assert result.builder.dashboard_extensions == []

    def test_no_pages_without_cli_routes(self, tmp_path) -> None:
        result = create_cli_server(CliServerOptions(cli_routes=False))
        result.mount_plugin(CliJobsBuilder().set_dashboard_dir(_build(tmp_path / "j")).build())
        assert result.builder.dashboard_extensions == []

    def test_the_registrations_match_between_the_core_and_the_plugins(self) -> None:
        for page in (JOBS_DASHBOARD_PAGE, AUDIT_DASHBOARD_PAGE, TOKENS_DASHBOARD_PAGE, FILESYSTEM_PAGE, DATA_EXPLORER_PAGE):
            CliDashboardExtension.of({**page, "entry": "/x.js"}).validated()
