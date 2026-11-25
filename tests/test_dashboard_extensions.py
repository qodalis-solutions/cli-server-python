"""Dashboard extensions (cli-server-dashboard ``docs/extensions.md``):
``builder.add_dashboard_extension``, a mounted plugin's ``dashboard_extensions``
and ``GET /api/v1/qcli/dashboard/extensions`` (admin plugin)."""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qodalis_cli import CliServerOptions, create_cli_server  # noqa: E402
from qodalis_cli.extensions import CliBuilder  # noqa: E402
from qodalis_cli.models import CliDashboardExtension  # noqa: E402
from qodalis_cli.services import CliCommandRegistry  # noqa: E402
from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder  # noqa: E402
from qodalis_cli_users import CliUsersBuilder  # noqa: E402
from tests.test_admin_plugin import _StubEventSocketManager, _StubRegistry  # noqa: E402

MARKETPLACE = {
    "id": "marketplace",
    "title": "Marketplace",
    "titles": {"de": "Marktplatz", "pt-BR": "Mercado"},
    "icon": "🧩",
    "group": "data",
    "order": 10,
    "permission": "marketplace:moderate",
    "entry": "/qcli/marketplace/admin/extension.js",
    "styles": ["/qcli/marketplace/admin/extension.css"],
    "version": "0.1.0",
}


def _builder() -> CliBuilder:
    return CliBuilder(CliCommandRegistry())


class TestRegistration:
    def test_keeps_the_registration_order_and_the_json_shape(self) -> None:
        builder = _builder()
        builder.add_dashboard_extension(MARKETPLACE)
        builder.add_dashboard_extension(
            CliDashboardExtension(id="billing", title="Billing", entry="https://cdn.example.com/billing.js")
        )
        assert [e.to_dict() for e in builder.dashboard_extensions] == [
            MARKETPLACE,
            {"id": "billing", "title": "Billing", "entry": "https://cdn.example.com/billing.js"},
        ]

    def test_refuses_an_id_registered_twice(self) -> None:
        builder = _builder().add_dashboard_extension(MARKETPLACE)
        with pytest.raises(ValueError, match="^dashboard extension already registered: marketplace$"):
            builder.add_dashboard_extension({**MARKETPLACE, "title": "Again"})

    def test_removes_an_extension_at_runtime_and_its_id_can_be_registered_again(self) -> None:
        builder = _builder().add_dashboard_extension(MARKETPLACE)
        builder.add_dashboard_extension({"id": "billing", "title": "Billing", "entry": "/b.js"})
        assert builder.remove_dashboard_extension("marketplace") is True
        assert builder.remove_dashboard_extension("marketplace") is False
        assert [e.id for e in builder.dashboard_extensions] == ["billing"]
        builder.add_dashboard_extension({**MARKETPLACE, "title": "Again"})
        assert [e.title for e in builder.dashboard_extensions] == ["Billing", "Again"]

    async def test_audit_sink_is_the_config_audit_store_else_none(self) -> None:
        builder = _builder()
        assert builder.audit_sink is None
        logged: list = []

        class _Store:
            async def log(self, entry) -> None:  # noqa: ANN001
                logged.append(entry)

        builder.set_config_audit_store(_Store())
        await builder.audit_sink({"event": "x.y"})
        assert logged == [{"event": "x.y"}]

    def test_turns_an_unknown_group_into_system(self) -> None:
        builder = _builder().add_dashboard_extension({"id": "x", "title": "X", "entry": "/x.js", "group": "elsewhere"})
        assert builder.dashboard_extensions[0].group == "system"

    @pytest.mark.parametrize(
        ("extension", "reason"),
        [
            ({"id": "Bad Id", "title": "X", "entry": "/x.js"}, "id must match"),
            ({"id": "-x", "title": "X", "entry": "/x.js"}, "id must match"),
            ({"id": "x" * 41, "title": "X", "entry": "/x.js"}, "id must match"),
            ({"id": "x", "title": "", "entry": "/x.js"}, "title must be 1-40 characters"),
            ({"id": "x", "title": "y" * 41, "entry": "/x.js"}, "title must be 1-40 characters"),
            ({"id": "x", "title": "X", "entry": "//evil.example/x.js"}, "entry must be"),
            ({"id": "x", "title": "X", "entry": "javascript:alert(1)"}, "entry must be"),
            ({"id": "x", "title": "X", "entry": "x.js"}, "entry must be"),
            ({"id": "x", "title": "X", "entry": "/x\t.js"}, "entry must be"),
            ({"id": "x", "title": "X", "entry": "/x.js", "titles": {"not a code": "X"}}, "titles must map"),
            ({"id": "x", "title": "X", "entry": "/x.js", "icon": "<img>"}, "icon must be"),
            ({"id": "x", "title": "X", "entry": "/x.js", "icon": "ab"}, "icon must be"),
            ({"id": "x", "title": "X", "entry": "/x.js", "order": 1.5}, "order must be an integer"),
            ({"id": "x", "title": "X", "entry": "/x.js", "permission": ""}, "permission must be"),
            ({"id": "x", "title": "X", "entry": "/x.js", "styles": ["/a.css", "/b.css", "/c.css", "/d.css", "/e.css", "/f.css"]}, "styles must be"),
            ({"id": "x", "title": "X", "entry": "/x.js", "styles": ["//evil/a.css"]}, "styles must be"),
            ({"id": "x", "title": "X", "entry": "/x.js", "version": ""}, "version must be"),
        ],
    )
    def test_refuses_an_invalid_extension(self, extension: dict, reason: str) -> None:
        with pytest.raises(ValueError) as error:
            _builder().add_dashboard_extension(extension)
        assert str(error.value).startswith("invalid dashboard extension: ")
        assert reason in str(error.value)

    @pytest.mark.parametrize("icon", ["🧩", "🇷🇴", "👨‍👩‍👧", "🖥️", "/icons/billing.svg", "https://cdn.example.com/i.png"])
    def test_accepts_emoji_and_image_urls_as_icons(self, icon: str) -> None:
        builder = _builder().add_dashboard_extension({"id": "x", "title": "X", "entry": "/x.js", "icon": icon})
        assert builder.dashboard_extensions[0].icon == icon


class _Plugin:
    """A host plugin bringing its own dashboard page."""

    prefix = "/billing"
    cli_routes = False

    def __init__(self, extensions: list) -> None:
        self.router = APIRouter()
        self.dashboard_extensions = extensions


@pytest.fixture()
async def admin_client():
    builder = _builder()
    builder.add_dashboard_extension(MARKETPLACE)
    builder.add_dashboard_extension({"id": "billing", "title": "Billing", "entry": "/billing/admin.js"})
    admin = CliAdminBuilder().set_credentials("admin", "pw").set_jwt_secret("s" * 32).build(
        AdminBuildDeps(registry=_StubRegistry(), event_socket_manager=_StubEventSocketManager(), builder=builder)
    )
    app = FastAPI()
    app.include_router(admin.router, prefix="/api/v1/qcli")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestEndpoint:
    async def test_lists_the_extensions_in_registration_order(self, admin_client: AsyncClient) -> None:
        assert (await admin_client.get("/api/v1/qcli/dashboard/extensions")).status_code == 401
        login = await admin_client.post("/api/v1/qcli/auth/login", json={"username": "admin", "password": "pw"})
        admin_client.headers["Authorization"] = f"Bearer {login.json()['token']}"

        res = await admin_client.get("/api/v1/qcli/dashboard/extensions")

        assert res.status_code == 200
        assert res.headers["cache-control"] == "no-cache"
        assert res.json() == {
            "extensions": [MARKETPLACE, {"id": "billing", "title": "Billing", "entry": "/billing/admin.js"}]
        }

    def test_mount_plugin_registers_a_plugins_extensions(self) -> None:
        server = create_cli_server(CliServerOptions())
        server.builder.add_dashboard_extension(MARKETPLACE)
        server.mount_plugin(_Plugin([{"id": "billing", "title": "Billing", "entry": "/billing/admin.js"}]))
        assert [e.id for e in server.builder.dashboard_extensions] == ["marketplace", "billing"]
        with pytest.raises(ValueError, match="dashboard extension already registered: marketplace"):
            server.mount_plugin(_Plugin([MARKETPLACE]))
        with pytest.raises(ValueError, match="invalid dashboard extension: n: entry must be"):
            server.mount_plugin(_Plugin([{"id": "n", "title": "N", "entry": "n.js"}]))

    def test_needs_a_sign_in_on_a_protected_server(self) -> None:
        server = create_cli_server(CliServerOptions())
        users = CliUsersBuilder().set_default_admin_password("root-pass-123").build()
        server.mount_plugin(users)
        server.builder.add_dashboard_extension(MARKETPLACE)
        admin = CliAdminBuilder().set_jwt_secret("s" * 32).set_users_plugin(users).build(
            AdminBuildDeps(
                registry=server.registry,
                event_socket_manager=server.event_socket_manager,
                builder=server.builder,
            )
        )
        server.mount_plugin(admin)
        with TestClient(server.app) as client:
            assert client.get("/api/v1/qcli/dashboard/extensions").status_code == 401
            login = client.post("/api/v1/qcli/sessions", json={"username": "root", "password": "root-pass-123"})
            assert login.status_code in (200, 201)
            res = client.get(
                "/api/v1/qcli/dashboard/extensions",
                headers={"Authorization": f"Bearer {login.json()['token']}"},
            )
            assert res.status_code == 200
            assert [e["id"] for e in res.json()["extensions"]] == ["marketplace"]


class TestAssetRoutes:
    def test_serves_each_app_at_its_prefix_without_sign_in_and_unhandled_paths_fall_through(self) -> None:
        from starlette.responses import PlainTextResponse

        async def app(scope, receive, send) -> None:  # noqa: ANN001
            await PlainTextResponse("ok", media_type="text/javascript")(scope, receive, send)

        class _Asset:
            prefix = "/qcli/assets-test"
            handles = staticmethod(lambda path: path == "/hello.js")

        _Asset.app = staticmethod(app)

        class _Mounted:
            prefix = "/api/v1/qcli/x"
            router = APIRouter()
            asset_routes = [_Asset()]

        server = create_cli_server(CliServerOptions())
        server.mount_plugin(_Mounted())
        users = CliUsersBuilder().set_default_admin_password("root-pass-123").build()
        server.mount_plugin(users)
        with TestClient(server.app) as client:
            assert client.get("/qcli/assets-test/hello.js").text == "ok"
            assert client.get("/qcli/assets-test/other.js").status_code == 404
