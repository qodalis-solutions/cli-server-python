"""Comprehensive smoke tests for the admin dashboard plugin."""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from qodalis_cli_admin import (
    AdminBuildDeps,
    CliAdminBuilder,
    CliAdminPlugin,
)
from qodalis_cli_admin.auth.jwt_service import JwtService
from qodalis_cli_admin.services.admin_config import AdminConfig
from qodalis_cli_admin.services.log_ring_buffer import LogEntry, LogRingBuffer
from qodalis_cli_admin.services.module_registry import ModuleRegistry


# ---------------------------------------------------------------------------
# Stub dependencies
# ---------------------------------------------------------------------------

TEST_USERNAME = "testadmin"
TEST_PASSWORD = "testpass"
TEST_JWT_SECRET = "super-secret-key-for-tests"
PREFIX = "/api/v1/qcli"


class _StubProcessor:
    """Minimal processor stub."""

    command = "stub"
    description = "stub processor"


class _StubModule:
    """Minimal module stub with the interface expected by ModuleRegistry."""

    def __init__(self, name: str = "StubModule", processor_count: int = 2) -> None:
        self._name = name
        self._processors = [_StubProcessor() for _ in range(processor_count)]

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return "A stub module"

    @property
    def processors(self):
        return self._processors


class _StubBuilder:
    """Minimal CliBuilder stub — only needs a ``modules`` property."""

    def __init__(self, modules: list | None = None) -> None:
        self._modules = modules or []

    @property
    def modules(self):
        return list(self._modules)


class _StubEventSocketManager:
    """Minimal event socket manager stub."""

    def __init__(self, clients: list | None = None) -> None:
        self._clients = clients or []

    def get_clients(self) -> list:
        return self._clients


class _StubRegistry:
    """Minimal command registry stub."""

    def __init__(self) -> None:
        self.processors: list = []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def stub_builder() -> _StubBuilder:
    return _StubBuilder(modules=[_StubModule("ModuleAlpha", 3), _StubModule("ModuleBeta", 1)])


@pytest.fixture()
def stub_esm() -> _StubEventSocketManager:
    return _StubEventSocketManager(
        clients=[
            {"id": "ws-1", "remoteAddress": "127.0.0.1"},
            {"id": "ws-2", "remoteAddress": "10.0.0.1"},
        ]
    )


@pytest.fixture()
def admin_plugin(stub_builder, stub_esm) -> CliAdminPlugin:
    deps = AdminBuildDeps(
        registry=_StubRegistry(),
        event_socket_manager=stub_esm,
        builder=stub_builder,
    )
    return (
        CliAdminBuilder()
        .set_credentials(TEST_USERNAME, TEST_PASSWORD)
        .set_jwt_secret(TEST_JWT_SECRET)
        .build(deps)
    )


@pytest.fixture()
def app(admin_plugin: CliAdminPlugin) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_plugin.router, prefix=PREFIX)
    return app


@pytest.fixture()
async def client(app: FastAPI):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def get_token(client: AsyncClient) -> str:
    resp = await client.post(
        f"{PREFIX}/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200
    return resp.json()["token"]


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# A. Auth tests
# ---------------------------------------------------------------------------


class TestAuthLogin:
    async def test_login_success(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{PREFIX}/auth/login",
            json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert data["username"] == TEST_USERNAME
        assert "expiresIn" in data
        assert data["expiresIn"] > 0

    async def test_login_wrong_credentials(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{PREFIX}/auth/login",
            json={"username": "wrong", "password": "wrong"},
        )
        assert resp.status_code == 401

    async def test_login_missing_fields(self, client: AsyncClient) -> None:
        resp = await client.post(f"{PREFIX}/auth/login", json={})
        assert resp.status_code == 422

    async def test_login_missing_password(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{PREFIX}/auth/login", json={"username": "admin"}
        )
        assert resp.status_code == 422


class TestAuthMe:
    async def test_me_with_valid_token(self, client: AsyncClient) -> None:
        token = await get_token(client)
        resp = await client.get(f"{PREFIX}/auth/me", headers=auth_header(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == TEST_USERNAME
        assert "authenticatedAt" in data

    async def test_me_without_token(self, client: AsyncClient) -> None:
        resp = await client.get(f"{PREFIX}/auth/me")
        assert resp.status_code == 401

    async def test_me_with_invalid_token(self, client: AsyncClient) -> None:
        resp = await client.get(
            f"{PREFIX}/auth/me",
            headers=auth_header("not.a.real.token"),
        )
        assert resp.status_code == 401


class TestProtectedEndpointsRejectWithoutToken:
    """All protected endpoints should return 401 when no token is provided."""

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", f"{PREFIX}/status"),
            ("GET", f"{PREFIX}/plugins"),
            ("POST", f"{PREFIX}/plugins/0/toggle"),
            ("GET", f"{PREFIX}/config"),
            ("PUT", f"{PREFIX}/config"),
            ("GET", f"{PREFIX}/logs"),
            ("GET", f"{PREFIX}/ws/clients"),
            ("GET", f"{PREFIX}/auth/me"),
        ],
    )
    async def test_rejects_without_token(
        self, client: AsyncClient, method: str, path: str
    ) -> None:
        resp = await client.request(method, path)
        assert resp.status_code in (401, 422), f"{method} {path} returned {resp.status_code}"


# ---------------------------------------------------------------------------
# B. Status tests
# ---------------------------------------------------------------------------


class TestStatus:
    async def test_returns_expected_shape(self, client: AsyncClient) -> None:
        token = await get_token(client)
        resp = await client.get(f"{PREFIX}/status", headers=auth_header(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "uptimeSeconds" in data
        assert data["uptimeSeconds"] >= 0
        assert "memoryUsageMb" in data
        assert data["platform"] == "python"
        assert "platformVersion" in data
        assert "os" in data
        assert "activeWsConnections" in data
        assert data["activeWsConnections"] == 2  # from stub
        assert "startedAt" in data


# ---------------------------------------------------------------------------
# C. Plugins tests
# ---------------------------------------------------------------------------


class TestPlugins:
    async def test_list_modules(self, client: AsyncClient) -> None:
        token = await get_token(client)
        resp = await client.get(f"{PREFIX}/plugins", headers=auth_header(token))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["name"] == "_StubModule"
        assert data[0]["processorCount"] == 3
        assert data[0]["enabled"] is True
        assert data[1]["processorCount"] == 1

    async def test_toggle_module(self, client: AsyncClient) -> None:
        token = await get_token(client)
        # Toggle off
        resp = await client.post(
            f"{PREFIX}/plugins/0/toggle", headers=auth_header(token)
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False

        # Toggle back on
        resp = await client.post(
            f"{PREFIX}/plugins/0/toggle", headers=auth_header(token)
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    async def test_toggle_not_found(self, client: AsyncClient) -> None:
        token = await get_token(client)
        resp = await client.post(
            f"{PREFIX}/plugins/999/toggle", headers=auth_header(token)
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "PLUGIN_NOT_FOUND"


# ---------------------------------------------------------------------------
# D. Config tests
# ---------------------------------------------------------------------------


class TestConfig:
    async def test_get_config_sections(self, client: AsyncClient) -> None:
        token = await get_token(client)
        resp = await client.get(f"{PREFIX}/config", headers=auth_header(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "sections" in data
        sections = data["sections"]
        ids = {s["id"] for s in sections}
        assert "auth" in ids
        assert "runtime" in ids

    async def test_update_settings(self, client: AsyncClient) -> None:
        token = await get_token(client)
        resp = await client.put(
            f"{PREFIX}/config",
            headers=auth_header(token),
            json={"settings": {"logLevel": "DEBUG", "maxConnections": 50}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["message"] == "Configuration updated"
        # Verify settings persisted
        runtime_section = next(s for s in data["sections"] if s["id"] == "runtime")
        assert runtime_section["settings"]["logLevel"] == "DEBUG"
        assert runtime_section["settings"]["maxConnections"] == 50


# ---------------------------------------------------------------------------
# E. Logs tests
# ---------------------------------------------------------------------------


class TestLogs:
    async def test_query_empty(self, client: AsyncClient) -> None:
        token = await get_token(client)
        resp = await client.get(f"{PREFIX}/logs", headers=auth_header(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "entries" in data
        assert "total" in data
        assert isinstance(data["entries"], list)

    async def test_query_with_entries(
        self, client: AsyncClient, admin_plugin: CliAdminPlugin
    ) -> None:
        # Seed some log entries
        for i in range(5):
            admin_plugin.log_buffer.add(
                LogEntry(
                    timestamp=f"2025-01-01T00:00:0{i}Z",
                    level="INFO",
                    message=f"Test message {i}",
                    logger_name="test",
                )
            )
        token = await get_token(client)
        resp = await client.get(f"{PREFIX}/logs", headers=auth_header(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 5

    async def test_filter_by_level(
        self, client: AsyncClient, admin_plugin: CliAdminPlugin
    ) -> None:
        admin_plugin.log_buffer.add(
            LogEntry(timestamp="t1", level="INFO", message="info msg")
        )
        admin_plugin.log_buffer.add(
            LogEntry(timestamp="t2", level="ERROR", message="error msg")
        )
        admin_plugin.log_buffer.add(
            LogEntry(timestamp="t3", level="INFO", message="another info")
        )
        token = await get_token(client)
        resp = await client.get(
            f"{PREFIX}/logs", headers=auth_header(token), params={"level": "ERROR"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["entries"][0]["level"] == "ERROR"

    async def test_search_filter(
        self, client: AsyncClient, admin_plugin: CliAdminPlugin
    ) -> None:
        admin_plugin.log_buffer.add(
            LogEntry(timestamp="t1", level="INFO", message="database connection ok")
        )
        admin_plugin.log_buffer.add(
            LogEntry(timestamp="t2", level="INFO", message="user login success")
        )
        token = await get_token(client)
        resp = await client.get(
            f"{PREFIX}/logs",
            headers=auth_header(token),
            params={"search": "database"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert "database" in data["entries"][0]["message"]

    async def test_pagination(
        self, client: AsyncClient, admin_plugin: CliAdminPlugin
    ) -> None:
        for i in range(10):
            admin_plugin.log_buffer.add(
                LogEntry(timestamp=f"t{i}", level="INFO", message=f"msg {i}")
            )
        token = await get_token(client)
        resp = await client.get(
            f"{PREFIX}/logs",
            headers=auth_header(token),
            params={"limit": 3, "offset": 2},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["entries"]) == 3
        assert data["total"] >= 10
        assert data["limit"] == 3
        assert data["offset"] == 2


# ---------------------------------------------------------------------------
# F. WS Clients tests
# ---------------------------------------------------------------------------


class TestWsClients:
    async def test_returns_client_list(self, client: AsyncClient) -> None:
        token = await get_token(client)
        resp = await client.get(f"{PREFIX}/ws/clients", headers=auth_header(token))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["id"] == "ws-1"
        assert data[1]["id"] == "ws-2"


# ---------------------------------------------------------------------------
# G. Service unit tests
# ---------------------------------------------------------------------------


class TestAdminConfig:
    def test_validate_credentials_correct(self) -> None:
        config = AdminConfig(username="admin", password="secret")
        assert config.validate_credentials("admin", "secret") is True

    def test_validate_credentials_wrong(self) -> None:
        config = AdminConfig(username="admin", password="secret")
        assert config.validate_credentials("admin", "wrong") is False
        assert config.validate_credentials("wrong", "secret") is False

    def test_get_config_sections(self) -> None:
        config = AdminConfig(username="admin", password="secret", jwt_secret="abc")
        sections = config.get_config_sections()
        assert len(sections) == 2
        auth_section = next(s for s in sections if s["id"] == "auth")
        assert auth_section["settings"]["username"] == "admin"
        assert auth_section["settings"]["jwtSecretConfigured"] is True

    def test_update_and_get_settings(self) -> None:
        config = AdminConfig(username="a", password="b")
        config.update_settings({"key1": "val1", "key2": 42})
        settings = config.get_settings()
        assert settings == {"key1": "val1", "key2": 42}

    def test_jwt_secret_not_configured(self) -> None:
        config = AdminConfig(username="a", password="b", jwt_secret="")
        sections = config.get_config_sections()
        auth_section = next(s for s in sections if s["id"] == "auth")
        assert auth_section["settings"]["jwtSecretConfigured"] is False


class TestJwtService:
    def test_sign_and_verify(self) -> None:
        svc = JwtService(secret="test-secret")
        token = svc.sign_token({"username": "alice"}, expires_in=3600)
        payload = svc.verify_token(token)
        assert payload["username"] == "alice"
        assert "exp" in payload
        assert "iat" in payload

    def test_expired_token_raises(self) -> None:
        import jwt as pyjwt

        svc = JwtService(secret="test-secret")
        token = svc.sign_token({"username": "alice"}, expires_in=-1)
        with pytest.raises(pyjwt.ExpiredSignatureError):
            svc.verify_token(token)

    def test_invalid_token_raises(self) -> None:
        import jwt as pyjwt

        svc = JwtService(secret="test-secret")
        with pytest.raises(pyjwt.InvalidTokenError):
            svc.verify_token("garbage.token.value")

    def test_wrong_secret_raises(self) -> None:
        import jwt as pyjwt

        svc1 = JwtService(secret="secret-one")
        svc2 = JwtService(secret="secret-two")
        token = svc1.sign_token({"username": "bob"})
        with pytest.raises(pyjwt.InvalidSignatureError):
            svc2.verify_token(token)

    def test_default_expiry(self) -> None:
        svc = JwtService(secret="test-secret")
        token = svc.sign_token({"username": "charlie"})
        payload = svc.verify_token(token)
        expected_exp = int(time.time()) + JwtService.DEFAULT_EXPIRY_SECONDS
        assert abs(payload["exp"] - expected_exp) < 5


class TestLogRingBuffer:
    def test_add_and_query(self) -> None:
        buf = LogRingBuffer(max_size=100)
        buf.add(LogEntry(timestamp="t1", level="INFO", message="hello"))
        buf.add(LogEntry(timestamp="t2", level="ERROR", message="oops"))
        entries, total = buf.query()
        assert total == 2
        assert len(entries) == 2

    def test_level_filter(self) -> None:
        buf = LogRingBuffer(max_size=100)
        buf.add(LogEntry(timestamp="t1", level="INFO", message="a"))
        buf.add(LogEntry(timestamp="t2", level="ERROR", message="b"))
        buf.add(LogEntry(timestamp="t3", level="INFO", message="c"))
        entries, total = buf.query(level="ERROR")
        assert total == 1
        assert entries[0]["level"] == "ERROR"

    def test_search_filter(self) -> None:
        buf = LogRingBuffer(max_size=100)
        buf.add(LogEntry(timestamp="t1", level="INFO", message="startup complete"))
        buf.add(LogEntry(timestamp="t2", level="INFO", message="request handled"))
        entries, total = buf.query(search="startup")
        assert total == 1
        assert "startup" in entries[0]["message"]

    def test_pagination(self) -> None:
        buf = LogRingBuffer(max_size=100)
        for i in range(20):
            buf.add(LogEntry(timestamp=f"t{i}", level="INFO", message=f"msg {i}"))
        entries, total = buf.query(limit=5, offset=10)
        assert total == 20
        assert len(entries) == 5

    def test_ring_buffer_overflow(self) -> None:
        buf = LogRingBuffer(max_size=5)
        for i in range(10):
            buf.add(LogEntry(timestamp=f"t{i}", level="INFO", message=f"msg {i}"))
        entries, total = buf.query()
        assert total == 5  # Only last 5 kept
        assert entries[0]["message"] == "msg 5"

    def test_warn_level_normalization(self) -> None:
        buf = LogRingBuffer(max_size=100)
        buf.add(LogEntry(timestamp="t1", level="WARNING", message="a warning"))
        entries, total = buf.query()
        assert entries[0]["level"] == "WARN"

    def test_filter_warn_matches_warning(self) -> None:
        buf = LogRingBuffer(max_size=100)
        buf.add(LogEntry(timestamp="t1", level="WARNING", message="a warning"))
        buf.add(LogEntry(timestamp="t2", level="INFO", message="an info"))
        entries, total = buf.query(level="WARN")
        assert total == 1
        assert entries[0]["level"] == "WARN"

    def test_combined_filters(self) -> None:
        buf = LogRingBuffer(max_size=100)
        buf.add(LogEntry(timestamp="t1", level="ERROR", message="database error"))
        buf.add(LogEntry(timestamp="t2", level="ERROR", message="network error"))
        buf.add(LogEntry(timestamp="t3", level="INFO", message="database ok"))
        entries, total = buf.query(level="ERROR", search="database")
        assert total == 1
        assert entries[0]["message"] == "database error"


class TestModuleRegistry:
    def test_list_modules(self) -> None:
        builder = _StubBuilder([_StubModule("A", 2), _StubModule("B", 5)])
        reg = ModuleRegistry(builder)
        modules = reg.list()
        assert len(modules) == 2
        assert modules[0]["name"] == "_StubModule"
        assert modules[0]["processorCount"] == 2
        assert modules[0]["enabled"] is True
        assert modules[1]["processorCount"] == 5

    def test_toggle_module(self) -> None:
        builder = _StubBuilder([_StubModule("A", 1)])
        reg = ModuleRegistry(builder)
        result = reg.toggle("0")
        assert result["enabled"] is False
        result = reg.toggle("0")
        assert result["enabled"] is True

    def test_toggle_not_found(self) -> None:
        builder = _StubBuilder([_StubModule("A", 1)])
        reg = ModuleRegistry(builder)
        with pytest.raises(KeyError):
            reg.toggle("99")

    def test_list_empty(self) -> None:
        builder = _StubBuilder([])
        reg = ModuleRegistry(builder)
        assert reg.list() == []


# ---------------------------------------------------------------------------
# H. Rate limiting tests
# ---------------------------------------------------------------------------


class TestRateLimiting:
    async def test_rate_limit_after_failed_attempts(
        self, client: AsyncClient
    ) -> None:
        # Make 5 failed login attempts
        for i in range(5):
            resp = await client.post(
                f"{PREFIX}/auth/login",
                json={"username": "wrong", "password": "wrong"},
            )
            assert resp.status_code == 401, f"Attempt {i+1} should return 401"

        # 6th attempt should be rate limited
        resp = await client.post(
            f"{PREFIX}/auth/login",
            json={"username": "wrong", "password": "wrong"},
        )
        assert resp.status_code == 429

    async def test_rate_limit_blocks_valid_credentials_too(
        self, client: AsyncClient
    ) -> None:
        # Exhaust rate limit
        for _ in range(5):
            await client.post(
                f"{PREFIX}/auth/login",
                json={"username": "wrong", "password": "wrong"},
            )

        # Even valid credentials should be blocked
        resp = await client.post(
            f"{PREFIX}/auth/login",
            json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        )
        assert resp.status_code == 429

    async def test_successful_login_does_not_count(
        self, client: AsyncClient
    ) -> None:
        # Successful logins should not count toward rate limit
        for _ in range(3):
            resp = await client.post(
                f"{PREFIX}/auth/login",
                json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
            )
            assert resp.status_code == 200

        # Should still be able to login
        resp = await client.post(
            f"{PREFIX}/auth/login",
            json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        )
        assert resp.status_code == 200
