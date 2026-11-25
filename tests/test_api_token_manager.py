"""``users_plugin.api_tokens``: tokens a host creates, lists and revokes for
its users, with the ``POST /tokens`` validation, limits, audit and events."""

from __future__ import annotations

from typing import Any

import pytest

from qodalis_cli import CliPermission, CliServerOptions, create_cli_server
from qodalis_cli_users import (
    ApiTokenCreateOptions,
    ApiTokenManager,
    CliUsersBuilder,
    CreatedApiToken,
    ExternalAuthResult,
    User,
    UsersApiError,
)

ROOT_PASS = "rootpass1"
DAY_MS = 24 * 60 * 60 * 1000

HOST_PERMISSIONS = [
    CliPermission(name="rates:read", description="Official exchange rates", group="Rates"),
    CliPermission(name="banks:read", description="Banks and their buy and sell rates", group="Rates"),
]


async def _plugin(builder: CliUsersBuilder | None = None) -> Any:
    result = create_cli_server(CliServerOptions(configure=lambda b: b.add_permissions(HOST_PERMISSIONS)))
    users = (builder or CliUsersBuilder()).set_default_admin_password(ROOT_PASS).build()
    result.mount_plugin(users)
    await users.ensure_seeded()
    users.events = []
    result.event_bus.subscribe("token:*", lambda event: users.events.append(event))
    return users


async def _user(users: Any, name: str, perms: list[str], *, disabled: bool = False) -> User:
    await users.group_store.create_group(f"{name}-g", permissions=perms)
    return await users.user_store.create_user(name, f"{name}@example.com", [f"{name}-g"], disabled=disabled)


async def _fails(call, status: int, message: str) -> None:
    with pytest.raises(UsersApiError) as exc:
        await call
    assert (exc.value.status_code, exc.value.message) == (status, message)


@pytest.fixture()
async def users():
    return await _plugin()


@pytest.fixture()
async def alice(users):
    return await _user(users, "alice", ["rates:read", "files:read"])


def test_the_plugin_exposes_the_manager():
    users = CliUsersBuilder().build()
    assert isinstance(users.api_tokens, ApiTokenManager)


async def _audits(users: Any, event: str) -> list[Any]:
    return [e for e in await users.audit_store.get_entries() if e.event == event]


class TestCreate:
    async def test_cli_token(self, users, alice):
        created = await users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="ci"))
        assert isinstance(created, CreatedApiToken)
        assert created.token.startswith("qcli_pat_")
        view = created.api_token
        assert list(view) == ["id", "name", "userId", "userName", "createdAt", "expiresAt", "lastUsedAt", "audience"]
        assert (view["name"], view["userId"], view["userName"], view["audience"]) == ("ci", alice.id, "alice", "cli")
        assert abs(view["expiresAt"] - (view["createdAt"] + 90 * DAY_MS)) < 1000
        assert created.to_dict() == {"token": created.token, "apiToken": view}
        stored = await users.api_token_store.get_token(view["id"])
        assert stored.audience == "cli"
        audit = await _audits(users, "token_created")
        assert [(a.username, a.actor, a.actor_id, a.user_id) for a in audit] == [("alice", "alice", alice.id, alice.id)]
        assert [e.type for e in users.events] == ["token:created"]
        assert users.events[0].data["actor"] == "alice"

    async def test_host_token(self, users, alice):
        created = await users.api_tokens.create(
            alice.id, ApiTokenCreateOptions(name="rates", permissions=["rates:read"], audience="host"),
        )
        assert created.api_token["audience"] == "host"
        assert created.api_token["permissions"] == ["rates:read"]
        assert (await users.api_token_store.get_token(created.api_token["id"])).audience == "host"

    async def test_actor(self, users, alice):
        created = await users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="ci", actor="rates-app"))
        audit = await _audits(users, "token_created")
        assert [(a.username, a.actor) for a in audit] == [("alice", "rates-app")]
        assert users.events[0].data["actor"] == "rates-app"
        assert created.api_token["userName"] == "alice"

    async def test_expiry(self, users, alice):
        view = (await users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="a", expires_in_days=7))).api_token
        assert abs(view["expiresAt"] - (view["createdAt"] + 7 * DAY_MS)) < 1000

    async def test_never_expires(self):
        users = await _plugin(CliUsersBuilder().set_allow_non_expiring_tokens())
        alice = await _user(users, "alice", ["rates:read"])
        view = (await users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="a", never_expires=True))).api_token
        assert view["expiresAt"] is None

    async def test_never_expires_not_allowed(self, users, alice):
        await _fails(users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="a", never_expires=True)),
                     400, "non-expiring tokens are not allowed")

    async def test_expiry_conflict(self):
        users = await _plugin(CliUsersBuilder().set_allow_non_expiring_tokens())
        alice = await _user(users, "alice", ["rates:read"])
        await _fails(
            users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="a", never_expires=True, expires_in_days=7)),
            400, "expiresInDays and neverExpires cannot both be set",
        )

    @pytest.mark.parametrize("days", [0, 366, -1])
    async def test_invalid_expires_in_days(self, users, alice, days):
        await _fails(users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="a", expires_in_days=days)),
                     400, "expiresInDays must be an integer between 1 and 365")

    @pytest.mark.parametrize("name", ["", "bad/name", "x" * 65])
    async def test_invalid_name(self, users, alice, name):
        await _fails(users.api_tokens.create(alice.id, ApiTokenCreateOptions(name=name)),
                     400, "name must be 1-64 characters of [A-Za-z0-9._ -]")

    async def test_user_not_found(self, users):
        await _fails(users.api_tokens.create("nobody", ApiTokenCreateOptions(name="a")), 404, "user not found")

    async def test_user_disabled(self, users):
        bob = await _user(users, "bob", ["rates:read"], disabled=True)
        await _fails(users.api_tokens.create(bob.id, ApiTokenCreateOptions(name="a")), 403, "account disabled")

    async def test_user_checked_before_audience(self, users):
        await _fails(users.api_tokens.create("nobody", ApiTokenCreateOptions(name="a", audience="api")),
                     404, "user not found")

    @pytest.mark.parametrize("audience", ["api", "HOST", "", None, 1])
    async def test_invalid_audience(self, users, alice, audience):
        await _fails(users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="a", audience=audience)),
                     400, "audience must be cli or host")

    async def test_audience_checked_before_name(self, users, alice):
        await _fails(users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="", audience="api")),
                     400, "audience must be cli or host")

    async def test_invalid_permission_syntax(self, users, alice):
        await _fails(users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="a", permissions=["bad perm!"])),
                     400, "invalid permission: bad perm!")

    async def test_cli_token_accepts_unknown_and_wildcard_entries(self, users):
        carol = await _user(users, "carol", ["deploy:*"])
        view = (await users.api_tokens.create(
            carol.id, ApiTokenCreateOptions(name="a", permissions=["deploy:*", "deploy:run"]),
        )).api_token
        assert view["permissions"] == ["deploy:*", "deploy:run"]

    async def test_cli_token_permissions_must_be_held(self, users, alice):
        await _fails(users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="a", permissions=["users:read"])),
                     403, "forbidden: cannot grant users:read")

    @pytest.mark.parametrize("permissions", [None, []])
    async def test_host_token_needs_permissions(self, users, alice, permissions):
        await _fails(
            users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="a", audience="host", permissions=permissions)),
            400, "host tokens need permissions",
        )

    @pytest.mark.parametrize("permission", ["files:read", "rates:*", "*", "unknown:read", "users:read"])
    async def test_not_a_host_permission(self, users, alice, permission):
        await _fails(
            users.api_tokens.create(
                alice.id, ApiTokenCreateOptions(name="a", audience="host", permissions=["rates:read", permission]),
            ),
            400, f"not a host permission: {permission}",
        )

    async def test_host_check_before_grant_check(self, users, alice):
        # banks:read is not held, files:read is not a host permission: 400 first.
        await _fails(
            users.api_tokens.create(
                alice.id, ApiTokenCreateOptions(name="a", audience="host", permissions=["banks:read", "files:read"]),
            ),
            400, "not a host permission: files:read",
        )

    async def test_create_host_normalizes_case(self, users, alice):
        view = (await users.api_tokens.create(
            alice.id, ApiTokenCreateOptions(name="a", audience="host", permissions=["RATES:READ"]),
        )).api_token
        assert view["permissions"] == ["rates:read"]
        stored = await users.api_token_store.get_token(view["id"])
        assert stored.permissions == ["rates:read"]

    async def test_create_host_held_through_wildcard(self, users):
        dave = await _user(users, "dave", ["rates:*", "banks:*"])
        created = await users.api_tokens.create(
            dave.id, ApiTokenCreateOptions(name="a", audience="host", permissions=["rates:read", "Banks:Read"]),
        )
        assert created.api_token["permissions"] == ["rates:read", "banks:read"]
        stored = await users.api_token_store.get_token(created.api_token["id"])
        assert stored.permissions == ["rates:read", "banks:read"]

    async def test_wildcard_does_not_reach_another_area(self, users):
        erin = await _user(users, "erin", ["rates:*"])
        await _fails(
            users.api_tokens.create(erin.id, ApiTokenCreateOptions(name="a", audience="host", permissions=["banks:read"])),
            403, "forbidden: cannot grant banks:read",
        )

    async def test_host_permission_not_held(self, users, alice):
        await _fails(
            users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="a", audience="host", permissions=["banks:read"])),
            403, "forbidden: cannot grant banks:read",
        )

    async def test_duplicate_name_across_audiences(self, users, alice):
        await users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="Rates", audience="host", permissions=["rates:read"]))
        await _fails(users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="rates")), 409, "token 'rates' already exists")

    async def test_limit_counts_both_audiences(self, users, alice):
        for i in range(10):
            await users.api_tokens.create(alice.id, ApiTokenCreateOptions(name=f"c{i}"))
            await users.api_tokens.create(
                alice.id, ApiTokenCreateOptions(name=f"h{i}", audience="host", permissions=["rates:read"]),
            )
        await _fails(users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="one-more")),
                     400, "a user may have at most 20 tokens")

    async def test_external_auth_without_api_tokens(self):
        users = await _plugin(CliUsersBuilder().set_auth_adapter(
            lambda req: ExternalAuthResult(user=User(id="ext", name="ext", groups=["users"]))))
        alice = await _user(users, "alice", ["rates:read"])
        # Checked first: even a missing user gets 501.
        await _fails(users.api_tokens.create("nobody", ApiTokenCreateOptions(name="a")),
                     501, "Authentication is managed by the host application")
        await _fails(users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="a")),
                     501, "Authentication is managed by the host application")

    async def test_external_auth_with_api_tokens(self):
        users = await _plugin(CliUsersBuilder().set_auth_adapter(
            lambda req: None).set_allow_api_tokens())
        alice = await _user(users, "alice", ["rates:read"])
        created = await users.api_tokens.create(
            alice.id, ApiTokenCreateOptions(name="a", audience="host", permissions=["rates:read"]),
        )
        assert created.api_token["audience"] == "host"


class TestListAndRevoke:
    async def test_list_oldest_first_both_audiences(self, users, alice):
        bob = await _user(users, "bob", ["rates:read"])
        await users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="first"))
        await users.api_tokens.create(bob.id, ApiTokenCreateOptions(name="bobs"))
        await users.api_tokens.create(
            alice.id, ApiTokenCreateOptions(name="second", audience="host", permissions=["rates:read"]),
        )
        listed = await users.api_tokens.list(alice.id)
        assert [(t["name"], t["audience"]) for t in listed] == [("first", "cli"), ("second", "host")]
        assert await users.api_tokens.list("nobody") == []

    async def test_revoke(self, users, alice):
        created = await users.api_tokens.create(
            alice.id, ApiTokenCreateOptions(name="rates", audience="host", permissions=["rates:read"]),
        )
        token_id = created.api_token["id"]
        assert await users.api_tokens.revoke(alice.id, token_id) is True
        assert await users.api_token_store.get_token(token_id) is None
        audit = await _audits(users, "token_revoked")
        assert [(a.username, a.actor, a.actor_id) for a in audit] == [("alice", "alice", alice.id)]
        assert [e.type for e in users.events] == ["token:created", "token:revoked"]
        assert await users.api_tokens.revoke(alice.id, token_id) is False

    async def test_revoke_with_actor(self, users, alice):
        created = await users.api_tokens.create(alice.id, ApiTokenCreateOptions(name="ci"))
        assert await users.api_tokens.revoke(alice.id, created.api_token["id"], actor="rates-app") is True
        audit = await _audits(users, "token_revoked")
        assert [(a.username, a.actor) for a in audit] == [("alice", "rates-app")]

    async def test_revoke_other_users_token(self, users, alice):
        bob = await _user(users, "bob", ["rates:read"])
        created = await users.api_tokens.create(bob.id, ApiTokenCreateOptions(name="ci"))
        assert await users.api_tokens.revoke(alice.id, created.api_token["id"]) is False
        assert await users.api_token_store.get_token(created.api_token["id"]) is not None
        assert await users.api_tokens.revoke(alice.id, "missing") is False


class TestGrantable:
    async def test_host(self, users, alice):
        entries = await users.api_tokens.grantable_permissions(alice.id, "host")
        assert [e.name for e in entries] == ["rates:read"]
        assert entries[0].to_dict() == {
            "name": "rates:read", "description": "Official exchange rates", "group": "Rates", "surface": "host",
        }

    async def test_cli(self, users, alice):
        entries = await users.api_tokens.grantable_permissions(alice.id)
        assert [e.name for e in entries] == ["files:read", "rates:read"]

    async def test_wildcards(self, users):
        dave = await _user(users, "dave", ["rates:*", "banks:read"])
        assert [e.name for e in await users.api_tokens.grantable_permissions(dave.id, "host")] == [
            "rates:read", "banks:read",
        ]
        root = await users.user_store.get_user("root")
        assert len(await users.api_tokens.grantable_permissions(root.id)) == 15 + 12 + 2

    async def test_errors(self, users, alice):
        await _fails(users.api_tokens.grantable_permissions("nobody"), 404, "user not found")
        await _fails(users.api_tokens.grantable_permissions(alice.id, "api"), 400, "audience must be cli or host")


async def test_http_route_still_creates_cli_tokens_only(users, alice):
    # The HTTP POST /tokens path shares the helpers: covered in test_api_tokens.py.
    from qodalis_cli_users.controllers import tokens_controller

    assert tokens_controller.HOST_TOKENS_NEED_PERMISSIONS == "host tokens need permissions"
    assert tokens_controller.NOT_A_HOST_PERMISSION == "not a host permission: "
    assert tokens_controller.EXPIRY_CONFLICT == "expiresInDays and neverExpires cannot both be set"
