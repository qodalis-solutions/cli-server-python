"""The config API (admin plugin over the server's config providers): GET
/config, GET /config/{provider}, PATCH /config/{provider}/{section}:
200/400/401/403/404/409, on an open server (static admin) and on a server
protected by the users plugin (config:read / config:write, a provider's
required_permission, audit, config:changed).  Port of the Node
``config-api.test.ts``."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder
from qodalis_cli_jobs import CliJobsBuilder
from qodalis_cli_server_abstractions.audit import AuditQueryOptions
from qodalis_cli_server_abstractions.config import (
    CliConfigSectionDefinition,
    CliConfigSettingDefinition,
    CliConfigValidation,
    CliStoredConfigProvider,
)
from qodalis_cli_users import CliUsersBuilder

V1 = "/api/v1/qcli"
ROOT_PASS = "rootpass1"
JWT_SECRET = "jwt-secret-for-tests-that-is-long-enough-for-hmac"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def billing(required_permission: str | None = None) -> CliStoredConfigProvider:
    return CliStoredConfigProvider(
        id="vault" if required_permission else "billing",
        title="Vault" if required_permission else "Billing",
        required_permission=required_permission,
        sections=[
            CliConfigSectionDefinition(id="invoices", settings=[
                CliConfigSettingDefinition(
                    key="dueDays", type="number", default=30, validation=CliConfigValidation(min=1, max=90),
                ),
                CliConfigSettingDefinition(key="apiKey", type="secret"),
                CliConfigSettingDefinition(
                    key="currency", type="enum", default="EUR", validation=CliConfigValidation(options=["EUR", "USD"]),
                ),
                CliConfigSettingDefinition(key="webhook", type="url", default="https://hooks.example/a"),
                CliConfigSettingDefinition(key="region", type="string", mutable=False, default="eu"),
            ]),
        ],
    )


def _entry(view: dict[str, Any], key: str) -> dict[str, Any]:
    return next(e for e in view["entries"] if e["key"] == key)


def _provider(providers: list[dict[str, Any]], provider_id: str) -> dict[str, Any]:
    return next(p for p in providers if p["id"] == provider_id)


# ---------------------------------------------------------------------------
# Open server (static admin)
# ---------------------------------------------------------------------------


class _Open:
    def __init__(self, client: TestClient, result: Any, token: str, events: list[Any]) -> None:
        self.c, self.result, self.token, self.events = client, result, token, events

    @property
    def h(self) -> dict[str, str]:
        return bearer(self.token)


@pytest.fixture(scope="module")
def open_srv():
    result = create_cli_server(CliServerOptions(
        allowed_origins=["https://app.example"],
        mcp=True,
        configure=lambda b: b.add_config_provider(billing()),
    ))
    admin = CliAdminBuilder().set_credentials("admin", "adminpass").set_jwt_secret(JWT_SECRET).build(AdminBuildDeps(
        registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder,
    ))
    result.mount_plugin(CliJobsBuilder().build())
    result.mount_plugin(admin)
    events: list[Any] = []
    result.event_bus.subscribe("config:*", events.append)
    try:
        with TestClient(result.app) as client:
            res = client.post(f"{V1}/auth/login", json={"username": "admin", "password": "adminpass"})
            assert res.status_code == 200, res.text
            yield _Open(client, result, res.json()["token"], events)
    finally:
        admin.log_buffer.uninstall_handler()


class TestConfigApiOpenServer:
    def test_401_without_the_admin_session(self, open_srv: _Open):
        assert open_srv.c.get(f"{V1}/config").status_code == 401
        assert open_srv.c.patch(f"{V1}/config/custom/custom", json={"changes": {"a": 1}}).status_code == 401

    def test_get_config_every_provider_in_order_with_the_json_shape(self, open_srv: _Open):
        res = open_srv.c.get(f"{V1}/config", headers=open_srv.h)
        assert res.status_code == 200
        assert res.headers["cache-control"] == "no-store"
        body = res.json()
        assert sorted(body) == ["defaultStore", "providers"]
        assert body["defaultStore"] == {"kind": "memory", "title": "In memory", "detail": "", "persistent": False}
        providers = body["providers"]
        assert [p["id"] for p in providers] == [
            "server", "auth", "host", "cors", "mcp", "jobs", "logs", "dashboard-plugins", "billing", "custom",
        ]
        view = _provider(providers, "billing")
        assert list(view) == ["id", "title", "description", "order", "writable", "storage", "store", "sections"]
        assert view["storage"] == {"kind": "store", "description": "Changes are saved to its config store", "sources": []}
        assert view["store"] == {"kind": "memory", "title": "In memory", "detail": "", "persistent": False}
        storage = {p["id"]: p["storage"] for p in providers}
        assert storage["server"] == {"kind": "runtime", "description": "Read from the running server process", "sources": []}
        assert storage["host"] == {
            "kind": "options",
            "description": "Server options and environment variables",
            "sources": ["Server options", "Environment variables"],
        }
        assert storage["auth"]["kind"] == "options"
        assert storage["auth"]["sources"] == []
        for provider_id in ("cors", "mcp", "jobs", "logs", "dashboard-plugins", "custom"):
            assert storage[provider_id]["kind"] == "store", provider_id
        assert view["title"] == "Billing"
        assert view["description"] == ""
        assert view["order"] == 100
        assert view["writable"] is True
        section = view["sections"][0]
        assert sorted(section) == ["allowCustomKeys", "description", "entries", "id", "mutable", "title", "version"]
        assert section["id"] == "invoices"
        assert section["title"] == "invoices"
        assert section["mutable"] is True
        assert section["version"] == "0"
        assert section["allowCustomKeys"] is False
        assert section["entries"][0] == {
            "key": "dueDays",
            "label": "dueDays",
            "description": "",
            "type": "number",
            "value": 30,
            "default": 30,
            "source": "default",
            "origin": "default",
            "mutable": True,
            "requiresRestart": False,
            "sensitive": False,
            "validation": {"min": 1, "max": 90},
        }
        api_key = section["entries"][1]
        assert {k: api_key[k] for k in ("key", "type", "value", "default", "sensitive", "isSet")} == {
            "key": "apiKey", "type": "secret", "value": None, "default": None, "sensitive": True, "isSet": False,
        }

    def test_get_one_provider_and_404_for_an_unknown_one(self, open_srv: _Open):
        res = open_srv.c.get(f"{V1}/config/server", headers=open_srv.h)
        assert res.status_code == 200
        body = res.json()
        assert body["id"] == "server"
        assert body["writable"] is False
        assert _entry(body["sections"][0], "platform")["value"] == "python"
        missing = open_srv.c.get(f"{V1}/config/nope", headers=open_srv.h)
        assert missing.status_code == 404
        assert missing.json() == {"error": "unknown config provider: nope"}

    def test_patch_200_with_the_section_applied_keys_version_and_visible(self, open_srv: _Open):
        res = open_srv.c.patch(
            f"{V1}/config/billing/invoices", headers=open_srv.h,
            json={"changes": {"dueDays": 45, "currency": "USD"}, "version": "0"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["provider"] == "billing"
        assert body["applied"] == ["dueDays", "currency"]
        assert body["requiresRestart"] is False
        assert body["version"] == "1"
        assert body["section"]["version"] == "1"
        due = _entry(body["section"], "dueDays")
        assert due["value"] == 45
        assert due["source"] == "runtime"
        again = open_srv.c.get(f"{V1}/config/billing", headers=open_srv.h)
        assert again.status_code == 200
        assert _entry(again.json()["sections"][0], "currency")["value"] == "USD"

    def test_patch_409_for_a_stale_version_with_the_current_one(self, open_srv: _Open):
        res = open_srv.c.patch(
            f"{V1}/config/billing/invoices", headers=open_srv.h, json={"changes": {"dueDays": 50}, "version": "0"},
        )
        assert res.status_code == 409
        assert res.json() == {"error": "version conflict", "version": "1"}

    def test_patch_400_with_per_key_errors(self, open_srv: _Open):
        res = open_srv.c.patch(
            f"{V1}/config/billing/invoices", headers=open_srv.h,
            json={"changes": {"dueDays": 0, "currency": "GBP", "webhook": "not-a-url", "region": "us", "unknown": 1}},
        )
        assert res.status_code == 400
        assert res.json() == {
            "error": "validation failed",
            "errors": {
                "dueDays": "must be at least 1",
                "currency": "must be one of: EUR, USD",
                "webhook": "must be an absolute URL",
                "region": "read-only",
                "unknown": "unknown setting",
            },
        }

    def test_patch_400_for_bad_bodies_and_read_only_providers_404_for_unknown_sections(self, open_srv: _Open):
        c, h = open_srv.c, open_srv.h
        res = c.patch(f"{V1}/config/billing/invoices", headers=h, json={"dueDays": 3})
        assert res.status_code == 400
        assert res.json() == {"error": "changes must be a non-empty object"}
        assert c.patch(f"{V1}/config/billing/invoices", headers=h, json=[1]).status_code == 400
        res = c.patch(f"{V1}/config/server/runtime", headers=h, json={"changes": {"pid": 1}})
        assert res.status_code == 400
        assert res.json() == {"error": "config provider server is read-only"}
        res = c.patch(f"{V1}/config/billing/nope", headers=h, json={"changes": {"a": 1}})
        assert res.status_code == 404
        assert res.json() == {"error": "unknown config section: billing.nope"}
        assert c.patch(f"{V1}/config/nope/x", headers=h, json={"changes": {"a": 1}}).status_code == 404

    def test_secrets_set_and_replaced_never_returned_cleared_with_null(self, open_srv: _Open):
        c, h = open_srv.c, open_srv.h
        res = c.patch(f"{V1}/config/billing/invoices", headers=h, json={"changes": {"apiKey": "sk_live_top_secret"}})
        assert res.status_code == 200, res.text
        assert "sk_live_top_secret" not in res.text
        res = c.get(f"{V1}/config", headers=h)
        assert res.status_code == 200
        assert "sk_live_top_secret" not in res.text
        entry = _entry(_provider(res.json()["providers"], "billing")["sections"][0], "apiKey")
        assert entry["value"] is None
        assert entry["isSet"] is True
        patch = c.patch(f"{V1}/config/billing/invoices", headers=h, json={"changes": {"apiKey": "sk_live_other"}})
        assert patch.status_code == 200
        assert "sk_live_other" not in patch.text
        cleared = c.patch(f"{V1}/config/billing/invoices", headers=h, json={"changes": {"apiKey": None}})
        assert cleared.status_code == 200
        entry = _entry(cleared.json()["section"], "apiKey")
        assert entry["value"] is None
        assert entry["isSet"] is False

    def test_custom_bag_as_a_provider_add_change_remove_keys(self, open_srv: _Open):
        c, h = open_srv.c, open_srv.h
        before = c.get(f"{V1}/config/custom", headers=h)
        assert before.status_code == 200
        section = before.json()["sections"][0]
        assert section["id"] == "custom"
        assert section["allowCustomKeys"] is True
        assert section["version"] == "0"
        assert section["entries"] == []
        res = c.patch(
            f"{V1}/config/custom/custom", headers=h, json={"changes": {"theme": "dark", "maxLogSize": 500}, "version": "0"},
        )
        assert res.status_code == 200, res.text
        res = c.patch(f"{V1}/config/custom/custom", headers=h, json={"changes": {"theme": None}, "version": "1"})
        assert res.status_code == 200, res.text
        assert [[e["key"], e["value"]] for e in res.json()["section"]["entries"]] == [["maxLogSize", 500]]

    def test_publishes_config_changed_on_every_change(self, open_srv: _Open):
        count = len(open_srv.events)
        res = open_srv.c.patch(f"{V1}/config/billing/invoices", headers=open_srv.h, json={"changes": {"dueDays": 20}})
        assert res.status_code == 200
        assert len(open_srv.events) == count + 1
        event = open_srv.events[-1]
        assert event.type == "config:changed"
        assert event.audience.permission == "config:read"
        assert {k: event.data.get(k) for k in ("provider", "section", "keys", "user")} == {
            "provider": "billing", "section": "invoices", "keys": ["dueDays"], "user": "admin",
        }

    def test_the_old_put_is_gone(self, open_srv: _Open):
        # Node answers 404; FastAPI answers 405 for a path served with other methods: either way, no PUT
        res = open_srv.c.put(f"{V1}/config", headers=open_srv.h, json={"theme": "dark"})
        assert res.status_code in (404, 405)


# ---------------------------------------------------------------------------
# Protected server (users plugin)
# ---------------------------------------------------------------------------


class _Protected:
    def __init__(self, client: TestClient, result: Any, users: Any, tokens: dict[str, str]) -> None:
        self.c, self.result, self.users, self.tokens = client, result, users, tokens

    def h(self, name: str) -> dict[str, str]:
        return bearer(self.tokens[name])

    def audit(self, event: str) -> list[Any]:
        return self.c.portal.call(self.users.audit_store.get_entries, AuditQueryOptions(event=event))


@pytest.fixture(scope="module")
def protected_srv():
    def configure(b):
        b.add_config_provider(billing())
        b.add_config_provider(billing("vault:admin"))

    result = create_cli_server(CliServerOptions(configure=configure))
    users = (
        CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(10_000).set_min_password_length(4).build()
    )
    admin = CliAdminBuilder().set_jwt_secret(JWT_SECRET).set_users_plugin(users).build(AdminBuildDeps(
        registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder,
    ))
    result.mount_plugin(admin)
    result.mount_plugin(users)
    tokens: dict[str, str] = {}
    try:
        with TestClient(result.app) as client:
            def login(username: str, password: str) -> str:
                res = client.post(f"{V1}/sessions", json={"username": username, "password": password})
                assert res.status_code == 201, f"login {username}: {res.status_code} {res.text}"
                return res.json()["token"]

            tokens["root"] = login("root", ROOT_PASS)

            def group(name: str, permissions: list[str]) -> None:
                res = client.post(f"{V1}/groups", headers=bearer(tokens["root"]),
                                  json={"name": name, "permissions": permissions})
                assert res.status_code == 201, res.text

            def user(name: str, groups: list[str]) -> None:
                res = client.post(f"{V1}/users", headers=bearer(tokens["root"]), json={
                    "name": name, "email": f"{name}@x.io", "password": f"{name}pass", "groups": groups,
                })
                assert res.status_code == 201, res.text

            group("noconfig", ["admin:access"])
            group("readers", ["admin:access", "config:read"])
            group("writers", ["admin:access", "config:read", "config:write"])
            group("vaulters", ["admin:access", "config:read", "config:write", "vault:admin"])
            user("nora", ["noconfig"])
            user("rita", ["readers"])
            user("will", ["writers"])
            user("vera", ["vaulters"])
            for name in ("nora", "rita", "will", "vera"):
                tokens[name] = login(name, f"{name}pass")
            yield _Protected(client, result, users, tokens)
    finally:
        admin.log_buffer.uninstall_handler()


class TestConfigApiProtectedServer:
    def test_401_without_a_session(self, protected_srv: _Protected):
        assert protected_srv.c.get(f"{V1}/config").status_code == 401

    def test_403_without_config_read(self, protected_srv: _Protected):
        res = protected_srv.c.get(f"{V1}/config", headers=protected_srv.h("nora"))
        assert res.status_code == 403
        assert "config:read" in res.json()["error"]

    def test_config_read_sees_providers_not_those_needing_another_permission_writable_false(
        self, protected_srv: _Protected,
    ):
        res = protected_srv.c.get(f"{V1}/config", headers=protected_srv.h("rita"))
        assert res.status_code == 200
        providers = res.json()["providers"]
        ids = [p["id"] for p in providers]
        assert "billing" in ids
        assert "users" in ids
        assert "vault" not in ids
        assert all(p["writable"] is False for p in providers)
        assert protected_srv.c.get(f"{V1}/config/vault", headers=protected_srv.h("rita")).status_code == 403

    def test_patch_needs_config_write(self, protected_srv: _Protected):
        res = protected_srv.c.patch(
            f"{V1}/config/billing/invoices", headers=protected_srv.h("rita"), json={"changes": {"dueDays": 5}},
        )
        assert res.status_code == 403
        assert "config:write" in res.json()["error"]

    def test_config_write_changes_and_a_provider_required_permission_guards_it_too(self, protected_srv: _Protected):
        c = protected_srv.c
        res = c.patch(
            f"{V1}/config/billing/invoices", headers=protected_srv.h("will"),
            json={"changes": {"dueDays": 5}, "version": "0"},
        )
        assert res.status_code == 200, res.text
        assert res.json()["version"] == "1"
        res = c.patch(f"{V1}/config/vault/invoices", headers=protected_srv.h("will"), json={"changes": {"dueDays": 5}})
        assert res.status_code == 403
        res = c.patch(f"{V1}/config/vault/invoices", headers=protected_srv.h("vera"), json={"changes": {"dueDays": 6}})
        assert res.status_code == 200, res.text
        writable = c.get(f"{V1}/config/billing", headers=protected_srv.h("will"))
        assert writable.status_code == 200
        assert writable.json()["writable"] is True

    def test_audits_config_update_in_the_users_audit_log_secrets_masked(self, protected_srv: _Protected):
        res = protected_srv.c.patch(
            f"{V1}/config/billing/invoices", headers=protected_srv.h("will"),
            json={"changes": {"apiKey": "never-log-me", "dueDays": 9}},
        )
        assert res.status_code == 200, res.text
        entries = protected_srv.audit("config.update")
        entry = next(
            e for e in entries
            if any(c.get("key") == "apiKey" for c in (e.properties or {}).get("changes", []))
        )
        assert entry.event == "config.update"
        assert entry.username == "will"
        assert entry.actor == "will"
        assert entry.target == "config:billing.invoices"
        assert entry.properties["changes"] == [
            {"key": "apiKey", "old": None, "new": "***"},
            {"key": "dueDays", "old": 5, "new": 9},
        ]
        assert entry.properties["via"] == "api"
        assert "never-log-me" not in json.dumps([e.to_dict() if hasattr(e, "to_dict") else vars(e) for e in entries],
                                                default=str)

    def test_the_users_provider_changes_the_session_timeout_of_the_running_plugin(self, protected_srv: _Protected):
        c, h = protected_srv.c, protected_srv.h("root")
        options = protected_srv.users.auth_service.options
        res = c.get(f"{V1}/config/users", headers=h)
        assert res.status_code == 200
        sessions = next(s for s in res.json()["sections"] if s["id"] == "sessions")
        assert _entry(sessions, "sessionTimeout")["value"] == "30m"
        res = c.patch(
            f"{V1}/config/users/sessions", headers=h,
            json={"changes": {"sessionTimeout": "2h"}, "version": sessions["version"]},
        )
        assert res.status_code == 200, res.text
        assert options.session_timeout_ms == 7_200_000
        bad = c.patch(f"{V1}/config/users/sessions", headers=h, json={"changes": {"sessionTimeout": "10s"}})
        assert bad.status_code == 400
        assert bad.json()["errors"] == {"sessionTimeout": "must be at least 1m"}
        ro = c.patch(f"{V1}/config/users/access", headers=h, json={"changes": {"protectServer": False}})
        assert ro.status_code == 400
        assert ro.json()["error"] == "config section users.access is read-only"
        res = c.patch(f"{V1}/config/users/sessions", headers=h, json={"changes": {"sessionTimeout": None}})
        assert res.status_code == 200, res.text
        assert options.session_timeout_ms == 30 * 60_000

    def test_auth_provider_reports_the_users_sign_in_mode(self, protected_srv: _Protected):
        res = protected_srv.c.get(f"{V1}/config/auth", headers=protected_srv.h("rita"))
        assert res.status_code == 200
        assert _entry(res.json()["sections"][0], "mode")["value"] == "users"
