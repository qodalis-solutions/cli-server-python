"""GET /commands lists each command's own ``requiredPermissions``, sub-commands included (the
parents' and ``commands:execute`` are not repeated); a command without any has no
``requiredPermissions`` field."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli.processors import CliHttpCommandProcessor
from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder
from qodalis_cli_aws import AwsModule
from qodalis_cli_databases import CliDatabasesBuilder, CliMigrationDatabaseOptions, sqlite_connection

from .demo_processors import CliEchoCommandProcessor


def _commands() -> list[dict[str, Any]]:
    result = create_cli_server(
        CliServerOptions(
            configure=lambda b: b.add_module(AwsModule())
            .add_processor(CliHttpCommandProcessor())
            .add_processor(CliEchoCommandProcessor())
        )
    )
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    result.mount_plugin(
        CliDatabasesBuilder().add_database("main", CliMigrationDatabaseOptions(sqlite_connection(conn))).build()
    )
    admin = CliAdminBuilder().set_credentials("admin", "pass").set_jwt_secret("secret-for-permission-tests").build(
        AdminBuildDeps(registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder),
    )
    admin.log_buffer.uninstall_handler()
    result.mount_plugin(admin)
    with TestClient(result.app) as client:
        res = client.get("/api/v1/qcli/commands")
        assert res.status_code == 200
        return res.json()


def _at(commands: list[dict[str, Any]], *path: str) -> dict[str, Any]:
    """The descriptor at a command path, e.g. ``_at(cmds, "aws", "s3", "rm")``."""
    level: list[dict[str, Any]] = commands
    current: dict[str, Any] = {}
    for name in path:
        current = next(c for c in level if c["command"] == name)
        level = current.get("processors") or []
    return current


def test_root_commands_list_their_own_permissions():
    cmds = _commands()

    assert _at(cmds, "aws")["requiredPermissions"] == ["aws:read"]
    assert _at(cmds, "databases")["requiredPermissions"] == ["databases:read"]
    assert _at(cmds, "http")["requiredPermissions"] == ["http:request"]
    assert _at(cmds, "admin")["requiredPermissions"] == ["admin:access"]


def test_sub_commands_list_their_own_permissions_without_the_parents():
    cmds = _commands()

    assert _at(cmds, "aws", "s3", "rm")["requiredPermissions"] == ["aws:write"]
    assert _at(cmds, "aws", "configure", "set")["requiredPermissions"] == ["aws:write"]
    assert _at(cmds, "databases", "up")["requiredPermissions"] == ["databases:migrate"]
    assert _at(cmds, "admin", "logs")["requiredPermissions"] == ["logs:read"]
    # read-only sub-commands declare nothing of their own: aws:read comes from the root and is not repeated
    assert "requiredPermissions" not in _at(cmds, "aws", "s3", "ls")
    assert "requiredPermissions" not in _at(cmds, "admin", "status")


def test_a_command_without_permissions_has_no_field():
    cmds = _commands()

    assert "requiredPermissions" not in _at(cmds, "echo")
    assert "commands:execute" not in json.dumps(cmds)
