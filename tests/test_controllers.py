"""Integration tests for the HTTP controllers using FastAPI TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from qodalis_cli.create_cli_server import create_cli_server, CliServerOptions

from .conftest import EchoProcessor, FailingProcessor, V2OnlyProcessor


def _make_client() -> TestClient:
    """Create a TestClient with known processors registered."""

    def configure(builder):
        builder.add_processor(EchoProcessor())
        builder.add_processor(FailingProcessor())
        builder.add_processor(V2OnlyProcessor())

    result = create_cli_server(CliServerOptions(configure=configure))
    return TestClient(result.app)


@pytest.fixture()
def client() -> TestClient:
    return _make_client()


# ---------------------------------------------------------------------------
# Version discovery
# ---------------------------------------------------------------------------


class TestVersionEndpoints:
    def test_get_versions_returns_supported(self, client: TestClient) -> None:
        resp = client.get("/api/qcli/versions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["supportedVersions"] == [1, 2]
        assert data["preferredVersion"] == 2

    def test_v1_version(self, client: TestClient) -> None:
        resp = client.get("/api/v1/qcli/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "1.0.0"

    def test_v2_version(self, client: TestClient) -> None:
        resp = client.get("/api/v2/qcli/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["apiVersion"] == 2
        assert data["serverVersion"] == "2.0.0"


# ---------------------------------------------------------------------------
# Commands listing
# ---------------------------------------------------------------------------


class TestCommandsListing:
    def test_v1_commands_returns_all(self, client: TestClient) -> None:
        resp = client.get("/api/v1/qcli/commands")
        assert resp.status_code == 200
        commands = resp.json()
        names = [c["command"] for c in commands]
        assert "echo" in names
        assert "fail" in names
        assert "v2cmd" in names

    def test_v2_commands_returns_only_v2_plus(self, client: TestClient) -> None:
        resp = client.get("/api/v2/qcli/commands")
        assert resp.status_code == 200
        commands = resp.json()
        names = [c["command"] for c in commands]
        # v2cmd has api_version=2, should be included
        assert "v2cmd" in names
        # echo has api_version=1 (default), should NOT be included
        assert "echo" not in names
        assert "fail" not in names


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------


class TestCommandExecution:
    def test_v1_execute_known_command(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/qcli/execute",
            json={"command": "echo", "value": "hello world"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exitCode"] == 0
        assert any("hello world" in o.get("value", "") for o in data["outputs"])

    def test_v1_execute_unknown_command(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/qcli/execute",
            json={"command": "nonexistent"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exitCode"] == 1
        assert any("Unknown command" in o.get("value", "") for o in data["outputs"])

    def test_v1_execute_failing_command(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/qcli/execute",
            json={"command": "fail"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exitCode"] == 1
        assert any("boom" in o.get("value", "") for o in data["outputs"])

    def test_v2_execute_known_command(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v2/qcli/execute",
            json={"command": "v2cmd"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exitCode"] == 0

    def test_legacy_route_still_works(self, client: TestClient) -> None:
        """The unversioned /api/qcli/execute route should still respond."""
        resp = client.post(
            "/api/qcli/execute",
            json={"command": "echo", "value": "legacy"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exitCode"] == 0
