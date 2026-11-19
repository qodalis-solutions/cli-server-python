"""The public OpenAPI document and Swagger UI of the public-api plugin (C6)."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Any, Iterator

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from qodalis_cli_public_api import CliPublicApiBuilder
from qodalis_cli_users import ApiTokenCreateOptions

from tests.test_users_host_tokens import _make as _make_users

QCLI_TOKEN = {
    "type": "http",
    "scheme": "bearer",
    "bearerFormat": "qcli_pat",
    "description": "API token (qcli_pat_…), sent as Authorization: Bearer <token>",
}
DOC = "/qcli/api/openapi.json"


def _add_public_routes(router: APIRouter, api: Any, users: Any) -> None:
    """Added once the plugin is built (the guard is its own); the document is
    read from the router when first requested."""

    @router.get("/v1/rates", tags=["Rates"])
    async def rates(caller=Depends(api.guard("rates:read"))):
        return {"tokenId": caller.token_id}

    @router.get("/v1/banks")
    async def banks(ctx=Depends(users.require_host_token("banks:read"))):
        return {"banks": []}

    @router.get("/v1/ping")
    async def ping():
        return {"pong": True}

    @router.get("/v1/hidden", include_in_schema=False)
    async def hidden():
        return {}


@contextmanager
def _host(configure: Any = None, source: str = "router") -> Iterator[tuple[TestClient, Any, Any]]:
    result, users = _make_users()
    builder = CliPublicApiBuilder().set_users(users)
    router = APIRouter()
    if source == "router":
        builder.set_openapi(router)
    if configure is not None:
        configure(builder)
    api = builder.build()
    _add_public_routes(router, api, users)
    result.mount_plugin(api)
    result.app.include_router(router)

    @result.app.get("/api/v1/internal")
    async def internal():
        return {}

    with TestClient(result.app) as client:
        yield client, api, users


def _doc(client: TestClient, path: str = DOC, **kwargs: Any) -> dict:
    res = client.get(path, **kwargs)
    assert res.status_code == 200, res.text
    return res.json()


def test_document_has_only_public_routes():
    with _host() as (client, _, _):
        res = client.get(DOC)
        assert res.status_code == 200
        assert res.headers["content-type"] == "application/json; charset=utf-8"
        assert res.headers["cache-control"] == "no-cache"
        assert res.headers["access-control-allow-origin"] == "*"
        assert sorted(res.json()["paths"]) == ["/v1/banks", "/v1/ping", "/v1/rates"]


def test_document_security_scheme_and_requirement():
    with _host() as (client, _, _):
        doc = _doc(client)
        assert doc["components"]["securitySchemes"]["qcliToken"] == QCLI_TOKEN
        assert doc["security"] == [{"qcliToken": []}]


def test_document_required_permission():
    with _host() as (client, _, _):
        paths = _doc(client)["paths"]
        rates = paths["/v1/rates"]["get"]
        assert rates["x-required-permission"] == "rates:read"
        assert rates["x-host-token-only"] is True
        assert rates["tags"] == ["Rates"]
        banks = paths["/v1/banks"]["get"]
        assert (banks["x-required-permission"], banks["x-host-token-only"]) == ("banks:read", True)
        ping = paths["/v1/ping"]["get"]
        assert "x-required-permission" not in ping and "x-host-token-only" not in ping


def test_document_title_and_version():
    with _host() as (client, _, _):
        assert _doc(client)["info"] == {"title": "API", "version": "1.0.0"}
    with _host(lambda b: b.set_title("RateBell API").set_version("2.1.0")) as (client, _, _):
        info = _doc(client)["info"]
        assert (info["title"], info["version"]) == ("RateBell API", "2.1.0")


def test_document_host_values_win():
    source = {
        "openapi": "3.1.0",
        "info": {"title": "Rates", "version": "3"},
        "servers": [{"url": "https://api.example.com"}],
        "security": [{"own": []}],
        "components": {"securitySchemes": {"qcliToken": {"type": "apiKey", "in": "header", "name": "X-Key"}}},
        "paths": {"/v1/ping": {"get": {"x-required-permission": "custom", "responses": {}}}},
    }
    original = copy.deepcopy(source)
    for given in (source, lambda: source):
        with _host(lambda b, g=given: b.set_openapi(g), source="given") as (client, _, _):
            doc = _doc(client)
            assert doc["servers"] == [{"url": "https://api.example.com"}]
            assert doc["security"] == [{"own": []}]
            assert doc["components"]["securitySchemes"]["qcliToken"]["type"] == "apiKey"
            assert doc["paths"]["/v1/ping"]["get"]["x-required-permission"] == "custom"
            assert doc["info"] == {"title": "Rates", "version": "3"}
    assert source == original


def test_document_dict_gets_the_additions():
    source = {"openapi": "3.1.0", "info": {"title": "Rates", "version": "1"}, "paths": {}}
    with _host(lambda b: b.set_openapi(source), source="given") as (client, _, _):
        doc = _doc(client)
        assert doc["components"]["securitySchemes"]["qcliToken"] == QCLI_TOKEN
        assert doc["security"] == [{"qcliToken": []}]
        assert doc["servers"] == [{"url": "http://testserver"}]
    assert "servers" not in source and "components" not in source


def test_document_servers_from_request():
    with _host() as (client, _, _):
        assert _doc(client)["servers"] == [{"url": "http://testserver"}]
    with _host(lambda b: b.set_server_url("https://api.ratebell.app")) as (client, _, _):
        assert _doc(client)["servers"] == [{"url": "https://api.ratebell.app"}]


def test_document_from_a_fastapi_app():
    public = FastAPI()

    @public.get("/v2/quotes", tags=["Quotes"])
    async def quotes():
        return {}

    with _host(lambda b: b.set_openapi(public), source="given") as (client, _, _):
        doc = _doc(client)
        assert list(doc["paths"]) == ["/v2/quotes"]
        assert doc["info"] == {"title": "API", "version": "1.0.0"}


def test_document_ignores_credentials():
    with _host() as (client, _, users):
        async def make() -> str:
            await users.ensure_seeded()
            await users.group_store.create_group("g", permissions=["rates:read"])
            alice = await users.user_store.create_user("alice", "alice@example.com", ["users", "g"])
            created = await users.api_tokens.create(
                alice.id, ApiTokenCreateOptions(name="t", audience="host", permissions=["rates:read"]),
            )
            return created.token

        token = client.portal.call(make)
        for bearer in (token, "garbage", "qcli_pat_unknown"):
            assert client.get(DOC, headers={"Authorization": f"Bearer {bearer}"}).status_code == 200


def test_document_not_mutated_between_requests():
    with _host() as (client, _, _):
        first = _doc(client, headers={"host": "one.example"})
        second = _doc(client, headers={"host": "two.example"})
        assert first["servers"] == [{"url": "http://one.example"}]
        assert second["servers"] == [{"url": "http://two.example"}]


def test_x_code_samples_pass_through():
    samples = [{"lang": "curl", "source": "curl https://api.example.com/v1/ping"}]
    source = {
        "openapi": "3.1.0", "info": {"title": "x", "version": "1"},
        "paths": {"/v1/ping": {"get": {"x-codeSamples": samples, "responses": {}}}},
    }
    with _host(lambda b: b.set_openapi(source), source="given") as (client, _, _):
        assert _doc(client)["paths"]["/v1/ping"]["get"]["x-codeSamples"] == samples


def test_swagger_serves_ui():
    with _host() as (client, _, _):
        for path in ("/qcli/api/swagger", "/qcli/api/swagger/"):
            res = client.get(path)
            assert res.status_code == 200, path
            assert res.headers["content-type"].startswith("text/html")
            html = res.text
            assert "'/qcli/api/openapi.json'" in html
            assert html.count("openapi.json") == 1
            assert '"persistAuthorization": false' in html
            assert '"tryItOutEnabled": true' in html
            assert "<title>API</title>" in html
            assert "swagger-ui-bundle.js" in html


def test_swagger_assets_override():
    with _host(lambda b: b.set_swagger_assets("/static/sw.js", "/static/sw.css")) as (client, _, _):
        html = client.get("/qcli/api/swagger").text
        assert "/static/sw.js" in html and "/static/sw.css" in html


def test_other_paths_under_the_base_are_not_handled():
    with _host() as (client, _, _):
        assert client.get("/qcli/api/other").status_code == 404
        assert client.get("/qcli/api").status_code == 404


def test_document_answers_head():
    with _host() as (client, _, _):
        res = client.head(DOC)
        assert res.status_code == 200 and res.content == b""


def test_custom_base_path():
    with _host(lambda b: b.set_base_path("/developers/api")) as (client, api, _):
        assert client.get("/developers/api/openapi.json").status_code == 200
        assert client.get("/developers/api/swagger").status_code == 200
        assert client.get(DOC).status_code == 404
        assert api.portal_section["base_path"] == "/developers/api"


def test_base_path_under_api_is_public_on_a_protected_server():
    with _host(lambda b: b.set_base_path("/api/public")) as (client, _, _):
        assert client.get("/api/public/openapi.json").status_code == 200
        assert client.get("/api/public/swagger").status_code == 200


@pytest.mark.parametrize("path", ["", "/", "api", "/api/", "/a//b", "/a b", "/a?b", "/a#b", "/a\\b"])
def test_invalid_base_path(path: str):
    users = _make_users()[1]
    with pytest.raises(ValueError) as exc:
        CliPublicApiBuilder().set_users(users).set_base_path(path).build()
    assert str(exc.value) == f"invalid public-api base path: {path}"


def test_plugin_routes_stay_out_of_the_app_document():
    with _host() as (client, _, _):
        paths = client.get("/openapi.json").json()["paths"]
        assert not any(p.startswith("/qcli/api") for p in paths)


def test_without_a_source_the_document_is_empty():
    with _host(source="none") as (client, _, _):
        doc = _doc(client)
        assert doc["paths"] == {}
        assert doc["security"] == [{"qcliToken": []}]


def test_invalid_source():
    with pytest.raises(TypeError):
        CliPublicApiBuilder().set_openapi(42)


def test_root_path():
    result, users = _make_users()
    api = CliPublicApiBuilder().set_users(users).set_swagger_assets("/static/sw.js", "https://cdn.example/sw.css").build()
    result.mount_plugin(api)
    with TestClient(result.app, root_path="/svc") as client:
        html = client.get("/qcli/api/swagger").text
        assert "'/svc/qcli/api/openapi.json'" in html
        assert "/svc/static/sw.js" in html and "https://cdn.example/sw.css" in html
        assert client.get("/qcli/api/openapi.json").json()["servers"] == [{"url": "http://testserver/svc"}]


@pytest.mark.parametrize("root", ["//evil.example", "/\\evil.example"])
def test_invalid_root_path(root: str):
    result, users = _make_users()
    result.mount_plugin(CliPublicApiBuilder().set_users(users).build())
    with TestClient(result.app, root_path=root) as client:
        for path in ("/qcli/api/openapi.json", "/qcli/api/swagger"):
            res = client.get(path)
            assert (res.status_code, res.json()) == (400, {"error": "invalid path base"}), path


@pytest.mark.parametrize("doc", [{}, {"openapi": "3.1.0", "info": {}}, {"openapi": 3, "info": {}, "paths": {}}])
def test_invalid_document_fails_at_build(doc: dict):
    with pytest.raises(ValueError, match="^public-api: invalid OpenAPI document$"):
        CliPublicApiBuilder().set_openapi(doc)
