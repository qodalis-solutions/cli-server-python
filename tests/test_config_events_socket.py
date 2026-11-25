"""``config:changed`` end to end: a WebSocket client on /ws/v1/qcli/events receives it after
PATCH /api/v1/qcli/config/{provider}/{section} (the admin plugin's API) and after the ``config set`` command, on an
open server and on a protected one (a signed-in admin; a user without config:read gets nothing)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

from fastapi.testclient import TestClient

from qodalis_cli import create_cli_server
from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder

from .command_helpers import ROOT_PASS, V1, login, protect, run_command

MARKER = "test:marker"


@contextmanager
def start(protected: bool) -> Iterator[tuple[TestClient, Any, Any, str]]:
    result = create_cli_server()
    users = protect(result) if protected else None
    builder = CliAdminBuilder().set_credentials("admin", "adminpass").set_jwt_secret("secret-for-the-config-socket-test")
    if users is not None:
        builder.set_users_plugin(users)
    admin = builder.build(AdminBuildDeps(
        registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder,
    ))
    result.mount_plugin(admin)
    try:
        with TestClient(result.app) as client:
            if users is not None:
                token = login(client, "root", ROOT_PASS)
            else:
                res = client.post(f"{V1}/auth/login", json={"username": "admin", "password": "adminpass"})
                assert res.status_code == 200, res.text
                token = res.json()["token"]
            yield client, result, users, token
    finally:
        admin.log_buffer.uninstall_handler()


def config_events(client: TestClient, result: Any, ws: Any) -> list[dict[str, Any]]:
    """The ``config:changed`` messages received so far (a marker published to everyone ends the read)."""
    client.portal.call(result.event_bus.publish, MARKER, {}, None)
    messages: list[dict[str, Any]] = []
    while True:
        message = json.loads(ws.receive_text())
        if message["type"] == MARKER:
            return messages
        if message["type"] == "config:changed":
            messages.append(message)


def patch_cors(client: TestClient, token: str, origins: list[str]) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    current = client.get(f"{V1}/config/cors", headers=headers)
    assert current.status_code == 200, current.text
    version = current.json()["sections"][0]["version"]
    res = client.patch(f"{V1}/config/cors/cors", headers=headers, json={"changes": {"allowedOrigins": origins}, "version": version})
    assert res.status_code == 200, res.text


def test_open_server_delivered_after_patch_and_after_config_set():
    with start(False) as (client, result, _, token), client.websocket_connect("/ws/v1/qcli/events") as ws:
        assert json.loads(ws.receive_text())["type"] == "connected"
        patch_cors(client, token, ["https://a.example"])
        [first] = config_events(client, result, ws)
        assert first["provider"] == "cors" and first["section"] == "cors"
        assert first["keys"] == ["allowedOrigins"] and first["version"] == "1"
        assert "Updated cors.cors.allowedOrigins" in run_command(
            client, result, "config set cors.cors.allowedOrigins https://b.example",
        ).text
        [second] = config_events(client, result, ws)
        assert second["provider"] == "cors" and second["keys"] == ["allowedOrigins"] and second["version"] == "2"


def test_the_unversioned_path_gets_it_too():
    with start(False) as (client, result, _, _token), client.websocket_connect("/ws/qcli/events") as ws:
        ws.receive_text()
        run_command(client, result, "config set cors.cors.allowedOrigins https://c.example")
        [event] = config_events(client, result, ws)
        assert event["provider"] == "cors"


def test_protected_server_the_admin_gets_it_a_user_without_config_read_does_not():
    with start(True) as (client, result, users, admin_token):
        root = {"Authorization": f"Bearer {admin_token}"}
        assert client.post(f"{V1}/users", headers=root, json={
            "name": "plain", "email": "plain@x.io", "password": "plainpass", "groups": ["users"],
        }).status_code == 201
        plain_token = login(client, "plain", "plainpass")

        def ticket(token: str) -> str:
            res = client.post(f"{V1}/sessions/ws-ticket", headers={"Authorization": f"Bearer {token}"})
            assert res.status_code == 201, res.text
            return res.json()["ticket"]

        with client.websocket_connect(f"/ws/v1/qcli/events?ticket={ticket(admin_token)}") as admin_ws, \
                client.websocket_connect(f"/ws/v1/qcli/events?ticket={ticket(plain_token)}") as plain_ws:
            admin_ws.receive_text()
            plain_ws.receive_text()
            patch_cors(client, admin_token, ["https://a.example"])
            [first] = config_events(client, result, admin_ws)
            assert first["provider"] == "cors" and first["keys"] == ["allowedOrigins"] and first["user"] == "root"
            assert "Updated" in run_command(
                client, result, "config set cors.cors.allowedOrigins https://b.example", admin_token,
            ).text
            [second] = config_events(client, result, admin_ws)
            assert second["version"] == "2" and second["user"] == "root"
            assert config_events(client, result, plain_ws) == []
