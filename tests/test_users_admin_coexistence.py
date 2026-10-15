"""Regression: users and admin plugins mounted together must not collide.

Both plugins are mounted at /api/v1/qcli in the demo server. The admin
dashboard keeps /auth/login; the users plugin logs in via POST /sessions.
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder  # noqa: E402
from qodalis_cli_users import CliUsersBuilder  # noqa: E402
from tests.test_admin_plugin import (  # noqa: E402
    _StubBuilder,
    _StubEventSocketManager,
    _StubModule,
    _StubRegistry,
)

PREFIX = "/api/v1/qcli"
ADMIN_USER = "dashadmin"
ADMIN_PASS = "dashpass"
ROOT_PASS = "rootpass"


@pytest.fixture()
async def client():
    admin_plugin = (
        CliAdminBuilder()
        .set_credentials(ADMIN_USER, ADMIN_PASS)
        .set_jwt_secret("coexistence-test-secret")
        .build(
            AdminBuildDeps(
                registry=_StubRegistry(),
                event_socket_manager=_StubEventSocketManager(),
                builder=_StubBuilder(modules=[_StubModule()]),
            )
        )
    )
    users_plugin = CliUsersBuilder().set_default_admin_password(ROOT_PASS).build()
    await users_plugin.startup()

    app = FastAPI()
    # Same order as demo/main.py: admin first, then users
    app.include_router(admin_plugin.router, prefix=PREFIX)
    app.include_router(users_plugin.router, prefix=users_plugin.prefix)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await users_plugin.shutdown()


async def test_users_login_via_sessions(client: AsyncClient):
    res = await client.post(
        f"{PREFIX}/sessions", json={"username": "root", "password": ROOT_PASS}
    )
    assert res.status_code == 201
    body = res.json()
    assert body["token"]
    assert body["session"]["user"]["name"] == "root"


async def test_admin_login_still_at_auth(client: AsyncClient):
    res = await client.post(
        f"{PREFIX}/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS}
    )
    assert res.status_code == 200
    assert res.json()["token"]


async def test_users_token_accepted_by_users_endpoints(client: AsyncClient):
    login = await client.post(
        f"{PREFIX}/sessions", json={"username": "root", "password": ROOT_PASS}
    )
    res = await client.get(
        f"{PREFIX}/sessions/current",
        headers={"Authorization": f"Bearer {login.json()['token']}"},
    )
    assert res.status_code == 200
    assert res.json()["user"]["name"] == "root"
