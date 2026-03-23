"""Integration tests for the HTTP controllers using FastAPI TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from qodalis_cli.create_cli_server import create_cli_server, CliServerOptions

from .conftest import EchoProcessor, FailingProcessor, StreamProcessor, V2OnlyProcessor


def _make_client() -> TestClient:
    """Create a TestClient with known processors registered."""

    def configure(builder):
        builder.add_processor(EchoProcessor())
        builder.add_processor(FailingProcessor())
        builder.add_processor(V2OnlyProcessor())
        builder.add_processor(StreamProcessor())

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
        assert data["supportedVersions"] == [1]
        assert data["preferredVersion"] == 1

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


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _parse_sse(text: str) -> list[dict]:
    import json

    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_type = "message"
        data = ""
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_type = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if data:
            events.append({"event": event_type, "data": json.loads(data)})
    return events


# ---------------------------------------------------------------------------
# Streaming execution
# ---------------------------------------------------------------------------


class TestStreamExecution:
    def test_stream_known_command(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/qcli/execute/stream",
            json={"command": "echo", "value": "hello world"},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        events = _parse_sse(resp.text)
        output_events = [e for e in events if e["event"] == "output"]
        assert any("hello world" in e["data"].get("value", "") for e in output_events), (
            f"Expected 'hello world' in output events: {output_events}"
        )
        done_events = [e for e in events if e["event"] == "done"]
        assert done_events, "Expected a 'done' event"
        assert done_events[0]["data"]["exitCode"] == 0

    def test_stream_unknown_command(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/qcli/execute/stream",
            json={"command": "nonexistent"},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        events = _parse_sse(resp.text)
        error_events = [e for e in events if e["event"] == "error"]
        assert error_events, "Expected an 'error' event"
        assert any(
            "Unknown command" in e["data"].get("message", "") for e in error_events
        ), f"Expected 'Unknown command' in error events: {error_events}"

    def test_stream_capable_processor(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/qcli/execute/stream",
            json={"command": "stream-test"},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        events = _parse_sse(resp.text)
        output_events = [e for e in events if e["event"] == "output"]
        assert len(output_events) == 3, (
            f"Expected 3 output events, got {len(output_events)}: {output_events}"
        )
        values = [e["data"].get("value") for e in output_events]
        assert values == ["chunk1", "chunk2", "chunk3"], f"Unexpected values: {values}"
        done_events = [e for e in events if e["event"] == "done"]
        assert done_events, "Expected a 'done' event"
        assert done_events[0]["data"]["exitCode"] == 0
