"""The portal plugin: the prebuilt portal site at one mount, with
``portal.json`` (contract C8)."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import create_cli_server
from qodalis_cli_docs import CliDocsBuilder
from qodalis_cli_portal import CliPortalBuilder
from qodalis_cli_public_api import CliPublicApiBuilder

from tests.test_users_host_tokens import _make as _make_users

SITE = Path(__file__).parent / "fixtures" / "portal-site"
DOCS = Path(__file__).parent / "fixtures" / "docs"
BOTH = {
    "name": "RateBell API",
    "logo": None,
    "theme": {"mode": "system", "dark": {}, "light": {"primary": "#0a7"}},
    "footerLinks": [{"label": "Terms", "url": "/terms"}],
    "docs": {"title": "Guides", "pages": "/qcli/docs/pages"},
    "api": {"title": "API reference", "openapi": "/qcli/api/openapi.json", "swagger": "/qcli/api/swagger"},
}


def _portal() -> CliPortalBuilder:
    return (
        CliPortalBuilder()
        .set_site_dir(str(SITE))
        .set_name("RateBell API")
        .add_footer_link("Terms", "/terms")
        .set_theme({"light": {"primary": "#0a7"}})
    )


@contextmanager
def _host(
    docs: bool = True, api: bool = True,
    configure: Callable[[CliPortalBuilder], Any] | None = None,
    host_routes: Callable[[Any], None] | None = None,
    host_routes_after: Callable[[Any], None] | None = None,
    root_path: str = "",
) -> Iterator[TestClient]:
    result, users = _make_users()
    builder = _portal()
    if docs:
        docs_plugin = CliDocsBuilder().add_folder(str(DOCS)).build()
        result.mount_plugin(docs_plugin)
        builder.add_section(docs_plugin)
    if api:
        api_plugin = CliPublicApiBuilder().set_users(users).set_openapi({"openapi": "3.1.0", "info": {"title": "Stub", "version": "1"}, "paths": {}}).build()
        result.mount_plugin(api_plugin)
        builder.add_section(api_plugin)
    if configure is not None:
        configure(builder)
    if host_routes is not None:
        host_routes(result.app)
    result.mount_plugin(builder.build())
    if host_routes_after is not None:
        host_routes_after(result.app)
    with TestClient(result.app, root_path=root_path) as client:
        yield client


def test_portal_json_both_sections():
    with _host() as client:
        res = client.get("/qcli/portal/portal.json")
        assert res.status_code == 200
        assert res.headers["cache-control"] == "no-cache"
        assert res.json() == BOTH
        assert list(res.json()) == ["name", "logo", "theme", "footerLinks", "docs", "api"]


def test_portal_json_docs_only():
    with _host(api=False) as client:
        body = client.get("/qcli/portal/portal.json").json()
        assert body["api"] is None and body["docs"] == BOTH["docs"]


def test_portal_json_api_only():
    with _host(docs=False) as client:
        body = client.get("/qcli/portal/portal.json").json()
        assert body["docs"] is None and body["api"] == BOTH["api"]


def test_portal_json_nothing_published():
    with _host(docs=False, api=False) as client:
        body = client.get("/qcli/portal/portal.json").json()
        assert body["docs"] is None and body["api"] is None


def test_portal_json_custom_section_titles_and_paths():
    result, users = _make_users()
    docs = CliDocsBuilder().set_title("Docs").set_base_path("/developers/docs").build()
    api = CliPublicApiBuilder().set_users(users).set_title("Reference").set_base_path("/developers/api").build()
    portal = _portal().set_logo_url("https://example.com/logo.svg").add_section(docs).add_section(api).build()
    result.mount_plugin(portal)
    with TestClient(result.app) as client:
        body = client.get("/qcli/portal/portal.json").json()
        assert body["logo"] == "https://example.com/logo.svg"
        assert body["docs"] == {"title": "Docs", "pages": "/developers/docs/pages"}
        assert body["api"] == {
            "title": "Reference", "openapi": "/developers/api/openapi.json", "swagger": "/developers/api/swagger",
        }


@pytest.mark.parametrize("path", [
    "/qcli/portal", "/qcli/portal/", "/qcli/portal/guides/auth", "/qcli/portal/reference",
    "/qcli/portal/reference/Rates",
])
def test_index_for_site_routes(path: str):
    with _host() as client:
        res = client.get(path)
        assert res.status_code == 200
        assert res.headers["content-type"] == "text/html; charset=utf-8"
        assert res.headers["cache-control"] == "no-cache"
        assert '<base href="/qcli/portal/" />' in res.text
        assert '<base href="/" />' not in res.text
        head = client.head(path)
        assert head.status_code == 200 and head.content == b""


def test_assets_served():
    with _host() as client:
        res = client.get("/qcli/portal/assets/app.js")
        assert res.status_code == 200
        assert res.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert res.text == (SITE / "assets" / "app.js").read_text()
        res = client.get("/qcli/portal/favicon.svg")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("image/svg+xml")
        assert "cache-control" not in res.headers


@pytest.mark.parametrize("method,path", [
    ("GET", "/qcli/portal/other"),
    ("GET", "/qcli/portal/assets/missing.js"),
    ("GET", "/qcli/portal/assets/sub/app.js"),
    ("GET", "/qcli/portal/assets/..%2Findex.html"),
    ("GET", "/qcli/portal/assets/.hidden"),
    ("GET", "/qcli/portal/guides/a/b"),
    ("GET", "/qcli/portal/index.html"),
    ("POST", "/qcli/portal/"),
    ("POST", "/qcli/portal/portal.json"),
    ("DELETE", "/qcli/portal/assets/app.js"),
])
def test_other_paths_fall_through(method: str, path: str):
    with _host() as client:
        res = client.request(method, path)
        assert res.status_code == 404, (method, path, res.text)
        assert "portal" not in res.text


def _host_routes(app: Any) -> None:
    @app.get("/v1/rates")
    async def rates():
        return "rates"

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/assets/host.js")
    async def host_asset():
        return "host asset"


@pytest.mark.parametrize("order", ["before", "after"])
def test_root_mount_does_not_shadow_host_routes(order: str):
    kwargs = {"host_routes": _host_routes} if order == "before" else {"host_routes_after": _host_routes}
    with _host(configure=lambda b: b.set_path("/"), **kwargs) as client:
        for accept in ("application/json", "text/html"):
            assert client.get("/v1/rates", headers={"Accept": accept}).json() == "rates"
            assert client.get("/health", headers={"Accept": accept}).json() == {"ok": True}
        assert client.get("/assets/host.js").json() == "host asset"
        for path in ("/", "/guides/x", "/reference"):
            res = client.get(path)
            assert res.status_code == 200
            assert '<base href="/" />' in res.text
        assert client.get("/portal.json").status_code == 200
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/assets/missing.js").status_code == 404
        assert client.get("/nothing").status_code == 404


def test_path_base():
    with _host(root_path="/svc") as client:
        res = client.get("/qcli/portal/guides/auth")
        assert '<base href="/svc/qcli/portal/" />' in res.text
        body = client.get("/qcli/portal/portal.json").json()
        assert body["docs"]["pages"] == "/svc/qcli/docs/pages"
        assert body["api"]["openapi"] == "/svc/qcli/api/openapi.json"
        assert body["api"]["swagger"] == "/svc/qcli/api/swagger"


@pytest.mark.parametrize("root", ["//evil.example", "/\\evil.example"])
def test_invalid_path_base(root: str):
    with _host(root_path=root) as client:
        for path in ("/qcli/portal/", "/qcli/portal/portal.json"):
            res = client.get(path)
            assert (res.status_code, res.json()) == (400, {"error": "invalid path base"})


def test_no_site_serves_nothing(tmp_path: Path, caplog):
    result, _ = _make_users()
    with caplog.at_level(logging.WARNING):
        portal = CliPortalBuilder().set_site_dir(str(tmp_path)).build()
    result.mount_plugin(portal)
    with TestClient(result.app) as client:
        for path in ("/qcli/portal/", "/qcli/portal/guides/x", "/qcli/portal/portal.json", "/qcli/portal/favicon.svg"):
            assert client.get(path).status_code == 404, path
    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("[qcli] portal site not found; the portal is not served") == 1


def test_explicit_site_dir_is_used_alone(tmp_path: Path):
    (tmp_path / "assets").mkdir()
    portal = CliPortalBuilder().set_site_dir(str(tmp_path)).build()
    assert portal.site_dir is None


def test_site_resolved_from_a_bundled_or_sibling_copy():
    from qodalis_cli_portal.site_resolver import resolve_site_dir

    assert resolve_site_dir(str(SITE)) == str(SITE.resolve())
    assert resolve_site_dir(str(SITE / "missing")) is None


def _expect(configure: Callable[[CliPortalBuilder], Any], text: str) -> None:
    builder = CliPortalBuilder().set_site_dir(str(SITE))
    configure(builder)
    with pytest.raises(ValueError) as exc:
        builder.build()
    assert str(exc.value) == text


def test_invalid_options_throw():
    name_text = "portal name must be 1-80 characters without control characters"
    logo_text = "portal logo must be an http(s) URL, a root-relative path or a data:image URL"
    _expect(lambda b: b.set_name("x" * 81), name_text)
    _expect(lambda b: b.set_name(""), name_text)
    _expect(lambda b: b.set_name("a\nb"), name_text)
    _expect(lambda b: b.set_logo_url("javascript:alert(1)"), logo_text)
    _expect(lambda b: b.set_logo_url("//evil.example/x.png"), logo_text)
    _expect(lambda b: b.set_logo_url("data:image/png;base64," + "A" * 200_000), logo_text)
    _expect(lambda b: b.set_theme({"light": {"primary": "red;}"}}), "invalid portal colour light.primary: red;}")
    _expect(lambda b: b.set_theme({"dark": {"nope": "#fff"}}), "invalid portal colour dark.nope: #fff")
    _expect(lambda b: b.set_theme({"mode": "dim"}), "invalid portal theme mode: dim")

    def many(b: CliPortalBuilder) -> None:
        for i in range(21):
            b.add_footer_link(f"Link {i}", f"/l{i}")

    _expect(many, "invalid portal footer link: Link 20")
    _expect(lambda b: b.add_footer_link("FTP", "ftp://x"), "invalid portal footer link: FTP")
    _expect(lambda b: b.add_footer_link("", "/x"), "invalid portal footer link: ")
    _expect(lambda b: b.add_footer_link("x" * 81, "/x"), "invalid portal footer link: " + "x" * 81)
    for path in ("qcli", "/qcli/", "", "/a//b", "/a b", "/a?b", "/a#b", '/a"b', "/a<b", "/a>b", "/a\\b"):
        _expect(lambda b, p=path: b.set_path(p), f"invalid portal path: {path}")


def test_valid_options():
    portal = (
        CliPortalBuilder().set_site_dir(str(SITE))
        .set_logo_url("data:image/png;base64,iVBORw0KGgo=")
        .set_theme({"mode": "dark", "dark": {"primary_hover": " #123456 ", "text": "oklch(0.7 0.1 200)"}})
        .add_footer_link("Status", "https://status.example.com")
        .build()
    )
    assert portal.document["theme"] == {
        "mode": "dark", "dark": {"primaryHover": "#123456", "text": "oklch(0.7 0.1 200)"}, "light": {},
    }
    assert portal.document["name"] == "Documentation"


def test_duplicate_section_throws():
    docs = CliDocsBuilder().build()
    with pytest.raises(ValueError, match="^portal section already registered: docs$"):
        CliPortalBuilder().set_site_dir(str(SITE)).add_section(docs).add_section(CliDocsBuilder().build()).build()


def test_portal_routes_are_host_routes():
    portal = CliPortalBuilder().set_site_dir(str(SITE)).build()
    assert portal.cli_routes is False
    result = create_cli_server()
    result.mount_plugin(portal)
    assert "/qcli/portal" not in result.cli_route_prefixes


def test_portal_ignores_credentials():
    with _host() as client:
        res = client.get("/qcli/portal/portal.json", headers={"Authorization": "Bearer garbage"})
        assert res.status_code == 200


def _secret_routes(app: Any) -> None:
    @app.get("/api/developers/reference/x/secret")
    async def reference_secret():
        return "secret"

    @app.get("/api/developers/assets/secret")
    async def asset_secret():
        return "secret"

    @app.get("/api/developers/guides/a/b")
    async def guide_secret():
        return "secret"

    @app.get("/api/developers/other")
    async def other():
        return "secret"


@pytest.mark.parametrize("order", ["before", "after"])
def test_public_routes_under_api_on_a_protected_server(order: str):
    result, users = _make_users()
    if order == "before":
        _secret_routes(result.app)
    docs = CliDocsBuilder().add_folder(str(DOCS)).set_base_path("/api/docs").build()
    api = CliPublicApiBuilder().set_users(users).set_base_path("/api/public").build()
    portal = _portal().set_path("/api/developers").add_section(docs).add_section(api).build()
    for plugin in (docs, api, portal):
        result.mount_plugin(plugin)
    if order == "after":
        _secret_routes(result.app)
    assert "/api/developers/assets/*" in result.public_paths
    assert "/api/docs/pages/*" in result.public_paths
    with TestClient(result.app) as client:
        for path in (
            "/api/developers", "/api/developers/", "/api/developers/guides/auth", "/api/developers/reference",
            "/api/developers/reference/Rates", "/api/developers/portal.json", "/api/developers/assets/app.js",
            "/api/developers/favicon.svg", "/api/docs/pages", "/api/docs/pages/index",
            "/api/public/openapi.json", "/api/public/swagger",
        ):
            assert client.get(path).status_code == 200, path
            assert client.head(path).status_code == 200, path
        # Under the public paths the portal answers itself: a host route there
        # is never reached without a token.
        for path in (
            "/api/developers/reference/x/secret", "/api/developers/assets/secret", "/api/developers/guides/a/b",
        ):
            res = client.get(path)
            assert (res.status_code, res.json()) == (404, {"error": "not found"}), path
        # Outside them the server's sign-in applies as before.
        assert client.get("/api/developers/other").status_code == 401
        assert client.post("/api/developers/portal.json").status_code == 401
        assert client.get("/api/v1/qcli/commands").status_code == 401
        assert client.get("/api/host/rates").status_code == 401


def test_portal_path_holding_cli_routes_is_refused():
    for path in (
        "/api", "/ws", "/api/v1", "/api/v2", "/api/qcli", "/api/v1/qcli/portal", "/ws/qcli/x",
        "/API", "/API/V1", "/Api/Qcli", "/WS/v1/QCLI/x",
    ):
        _expect(lambda b, p=path: b.set_path(p), f"invalid portal path: {path}")
    CliPortalBuilder().set_site_dir(str(SITE)).set_path("/api/developers").build()


@pytest.mark.parametrize("paths", [
    ["/*"], ["/elsewhere/*"], ["/qcli/xy"], ["/qcli/x/p*"], ["/api/v1/qcli/commands"], ["/qcli/x/../y"],
])
def test_invalid_public_paths_fail_startup(paths: list[str]):
    from dataclasses import dataclass, field

    from fastapi import APIRouter

    @dataclass
    class Plugin:
        prefix: str = "/qcli/x"
        router: Any = field(default_factory=APIRouter)
        cli_routes: bool = False
        public_paths: list = field(default_factory=list)

    result = create_cli_server()
    with pytest.raises(ValueError, match="^invalid public path: "):
        result.mount_plugin(Plugin(public_paths=paths))


def test_public_path_on_a_cli_prefix_fails_startup():
    from dataclasses import dataclass, field

    from fastapi import APIRouter

    @dataclass
    class Plugin:
        prefix: str = "/api/v1/qcli/docs"
        router: Any = field(default_factory=APIRouter)
        cli_routes: bool = False
        public_paths: list = field(default_factory=lambda: ["/api/v1/qcli/docs/pages"])

    with pytest.raises(ValueError, match="^invalid public path: /api/v1/qcli/docs/pages$"):
        create_cli_server().mount_plugin(Plugin())


def test_docs_on_a_cli_prefix_fails_startup():
    result, _ = _make_users()
    with pytest.raises(ValueError, match="^invalid public path: "):
        result.mount_plugin(CliDocsBuilder().set_base_path("/api/v1/qcli/docs").build())


def test_root_portal_declares_no_public_paths():
    assert CliPortalBuilder().set_site_dir(str(SITE)).set_path("/").build().public_paths == []
    assert CliPortalBuilder().set_site_dir("/nonexistent").build().public_paths == []
