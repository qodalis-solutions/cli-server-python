"""``GET /api/v1/qcli/auth/mode`` tells the admin dashboard which sign-in to
show: its own password form, or the host application's sign-in when the users
plugin uses an external auth adapter (mirrors the .NET
``AdminAuthModeTests``)."""

from __future__ import annotations

from typing import Callable

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder
from qodalis_cli_admin.services.admin_config import AdminConfig, is_valid_sign_in_url
from qodalis_cli_users import CliUsersBuilder, ExternalAuthResult, User

ADMIN_USER = "dashadmin"
ADMIN_PASS = "dashpass"
HOST_USER_HEADER = "x-host-user"
MODE = "/api/v1/qcli/auth/mode"
LOGIN = "/api/v1/qcli/auth/login"
ME = "/api/v1/qcli/auth/me"


def _external_auth(request):
    """Host session: any request with ``X-Host-User`` is signed in as an admin."""
    name = request.headers.get(HOST_USER_HEADER)
    if not name:
        return None
    return ExternalAuthResult(user=User(id=f"ext-{name}", name=name, email=f"{name}@host.test"), is_admin=True)


@pytest.fixture()
def start():
    clients: list[TestClient] = []

    def factory(users: Callable[[CliUsersBuilder], CliUsersBuilder] | None,
                sign_in_url: str | None = None) -> TestClient:
        result = create_cli_server(CliServerOptions())
        builder = CliAdminBuilder().set_credentials(ADMIN_USER, ADMIN_PASS) \
            .set_jwt_secret("auth-mode-test-secret-that-is-long-enough-for-hmac") \
            .set_sign_in_url(sign_in_url)
        if users is not None:
            users_plugin = users(CliUsersBuilder().set_default_admin_password("rootpass1")).build()
            result.mount_plugin(users_plugin)
            builder.set_users_plugin(users_plugin)
        admin = builder.build(AdminBuildDeps(
            registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder,
        ))
        result.mount_plugin(admin)
        client = TestClient(result.app)
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def _mode(client: TestClient) -> dict:
    res = client.get(MODE)
    assert res.status_code == 200, res.text
    assert res.headers["cache-control"] == "no-store"
    return res.json()


def test_mode_without_users_plugin_is_password(start):
    assert _mode(start(None)) == {"mode": "password", "signInUrl": None}


def test_mode_users_plugin_with_passwords_is_password_and_public_on_protected_server(start):
    client = start(lambda b: b, sign_in_url="https://app.example.com/sign-in")
    assert _mode(client) == {"mode": "password", "signInUrl": "https://app.example.com/sign-in"}


def test_mode_external_auth_is_external_with_sign_in_url(start):
    client = start(lambda b: b.set_auth_adapter(_external_auth), sign_in_url="/sign-in")
    assert _mode(client) == {"mode": "external", "signInUrl": "/sign-in"}


def test_mode_external_auth_without_sign_in_url_returns_null(start):
    client = start(lambda b: b.set_auth_adapter(_external_auth))
    assert _mode(client) == {"mode": "external", "signInUrl": None}


def test_server_option_is_set_by_the_users_plugin():
    result = create_cli_server(CliServerOptions())
    assert result.options.external_authentication is False
    result.mount_plugin(CliUsersBuilder().build())
    assert result.options.external_authentication is False
    result.mount_plugin(CliUsersBuilder().set_auth_adapter(_external_auth).build())
    assert result.options.external_authentication is True


def test_mode_follows_server_options_given_in_deps():
    options = CliServerOptions(external_authentication=True)
    result = create_cli_server(options)
    admin = CliAdminBuilder().build(AdminBuildDeps(
        registry=result.registry, event_socket_manager=result.event_socket_manager,
        builder=result.builder, server_options=options,
    ))
    result.app.include_router(admin.router, prefix=admin.prefix)
    with TestClient(result.app) as client:
        assert _mode(client)["mode"] == "external"


def test_login_external_auth_static_credentials_return_501(start):
    client = start(lambda b: b.set_auth_adapter(_external_auth), sign_in_url="https://app.example.com/sign-in")
    res = client.post(LOGIN, json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert res.status_code == 501
    body = res.json()
    assert "token" not in body
    assert body["code"] == "EXTERNAL_AUTH"
    assert "https://app.example.com/sign-in" in body["error"]
    assert body["signInUrl"] == "https://app.example.com/sign-in"


def test_login_external_auth_without_sign_in_url_points_to_host_app(start):
    client = start(lambda b: b.set_auth_adapter(_external_auth))
    res = client.post(LOGIN, json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert res.status_code == 501
    assert "host application" in res.json()["error"]
    assert res.json()["signInUrl"] is None


def test_external_auth_host_session_reaches_admin_routes(start):
    client = start(lambda b: b.set_auth_adapter(_external_auth))
    assert client.get(ME).status_code == 401
    me = client.get(ME, headers={HOST_USER_HEADER: "alice"})
    assert me.status_code == 200, me.text
    assert me.json()["username"] == "alice"
    assert client.get("/api/v1/qcli/status", headers={HOST_USER_HEADER: "alice"}).status_code == 200


def test_login_password_mode_still_signs_in(start):
    client = start(None)
    res = client.post(LOGIN, json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert res.status_code == 200, res.text
    token = res.json()["token"]
    assert token
    assert client.get(ME, headers={"Authorization": f"Bearer {token}"}).status_code == 200


@pytest.mark.parametrize("url", [
    "https://app.example.com/sign-in",
    "http://localhost:5000/login?next=%2Fadmin",
    "/sign-in",
    "/",
])
def test_sign_in_url_valid(url):
    assert is_valid_sign_in_url(url)


@pytest.mark.parametrize("url", [
    "",
    " ",
    "sign-in",
    "//evil.example.com/sign-in",
    "/\\evil.example.com",
    "javascript:alert(1)",
    "ftp://example.com/sign-in",
    "https://",
    "https://app.example.com/sign in",
    "/sign-in\n",
    "https://app.example.com:99999/sign-in",
])
def test_sign_in_url_invalid(url):
    assert not is_valid_sign_in_url(url)


def test_sign_in_url_invalid_fails_at_startup():
    result = create_cli_server(CliServerOptions())
    with pytest.raises(ValueError, match="sign_in_url"):
        CliAdminBuilder().set_sign_in_url("javascript:alert(1)").build(AdminBuildDeps(
            registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder,
        ))


def test_sign_in_url_none_is_allowed():
    AdminConfig(username="a", password="b", sign_in_url=None).validate()
