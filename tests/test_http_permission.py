"""The built-in ``http`` command requires ``http:request`` (in core's permission catalog, group
Commands): refused without it on a protected server, run with it."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from qodalis_cli import CliHttpCommandProcessor, CliServerOptions, create_cli_server

from .command_helpers import add_users, protect, run_command


def test_http_requires_http_request():
    processor = CliHttpCommandProcessor()
    assert processor.required_permissions == ["http:request"]


def test_http_request_is_a_core_permission():
    entry = create_cli_server().builder.permissions.find("http:request")
    assert (entry.group, entry.description, entry.surface) == (
        "Commands",
        "Make HTTP requests from the server",
        "cli",
    )


def test_protected_server_refuses_http_without_http_request():
    result = create_cli_server(CliServerOptions(configure=lambda b: b.add_processor(CliHttpCommandProcessor())))
    protect(result)
    with TestClient(result.app) as client:
        tokens = add_users(client, {
            "runner": ["commands:execute"],
            "requester": ["commands:execute", "http:request"],
        })

        # A file:// URL is refused by the command itself: no request leaves the test
        line = "http get file:///etc/hostname"
        denied = run_command(client, result, line, tokens["runner"])
        assert denied.status == 403
        assert "http:request" in json.dumps(denied.body)

        allowed = run_command(client, result, line, tokens["requester"])
        assert allowed.status == 200
        assert "Unsupported URL scheme" in allowed.text
