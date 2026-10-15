"""Unit tests for the users plugin: permissions, hashing, stores, auth
service and plugin lifecycle."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest

from qodalis_cli_users import (
    AuditEntry,
    AuditQueryOptions,
    AuthService,
    AuthServiceOptions,
    CliUsersBuilder,
    InMemoryAuditStore,
    InMemoryCredentialStore,
    InMemoryGroupStore,
    InMemorySessionStore,
    InMemoryUserStore,
    SessionMetadata,
    UsersApiError,
    UsersPluginOptions,
    build_users_plugin,
    has_permission,
    merge_permissions,
    permission_matches,
)
from qodalis_cli_users.services import password_hasher
from qodalis_cli_users.services.auth_service import hash_token


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("granted,required,expected", [
    ("*", "users:create", True),
    ("*", "anything", True),
    ("users:*", "users:create", True),
    ("users:*", "users", True),  # same as cliPermissionMatches
    ("users:*", "groups:read", False),
    ("users:read", "users:read", True),
    ("USERS:READ", "users:read", True),
    ("users:read", "users:create", False),
    ("users", "users:read", False),
    ("users:read", "users", False),
    (" users:read ", "users:read", True),
    ("a:*", "a:b:c", True),
    # A non-last '*' matches exactly one segment (not everything below).
    ("*:read", "users:read", True),
    ("*:read", "users:delete", False),
    ("*:read", "users:read:x", False),
    ("*:read", "*", False),
    ("users:*:x", "users:read:x", True),
    ("users:*:x", "users:delete", False),
    ("users:*:x", "users:read:y", False),
])
def test_permission_matches(granted, required, expected):
    assert permission_matches(granted, required) is expected


@pytest.mark.parametrize("granted,required,expected", [
    ("*", "admin:access", True),
    ("admin:*", "admin:access", True),
    ("*:access", "admin:access", True),
    ("*:read", "admin:access", False),
    ("admin:*:x", "admin:access", False),
])
def test_admin_permission_matches(granted, required, expected):
    from qodalis_cli_admin.auth.auth_controller import _matches

    assert _matches(granted, required) is expected


def test_has_and_merge_permissions():
    assert has_permission(["users:read", "audit:*"], "audit:clear")
    assert not has_permission([], "users:read")
    assert not has_permission(None, "users:read")
    assert merge_permissions(["a:b", "C:d"], None, ["c:D", "e:f", " "]) == ["a:b", "C:d", "e:f"]


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def test_hash_format_and_verify():
    encoded, salt = password_hasher.hash_password("secret")
    prefix, iterations, salt_b64, hash_b64 = encoded.split("$")
    assert prefix == "pbkdf2" and iterations == "100000"
    assert salt_b64 == salt
    assert len(base64.b64decode(salt_b64)) == 16
    key = base64.b64decode(hash_b64)
    assert len(key) == 32
    assert key == hashlib.pbkdf2_hmac("sha256", b"secret", base64.b64decode(salt_b64), 100_000, 32)
    assert password_hasher.verify_password("secret", encoded)
    assert not password_hasher.verify_password("Secret", encoded)
    assert not password_hasher.needs_rehash(encoded)


def test_salt_is_random():
    a, _ = password_hasher.hash_password("same")
    b, _ = password_hasher.hash_password("same")
    assert a != b


def test_verify_rejects_garbage():
    assert not password_hasher.verify_password("x", "")
    assert not password_hasher.verify_password("x", "pbkdf2$abc$!!$!!")


def test_interoperable_with_other_servers():
    """A hash produced by another implementation of the same format verifies."""
    salt = bytes(range(16))
    key = hashlib.pbkdf2_hmac("sha256", b"p4ss", salt, 100_000, 32)
    encoded = f"pbkdf2$100000${base64.b64encode(salt).decode()}${base64.b64encode(key).decode()}"
    assert password_hasher.verify_password("p4ss", encoded)


async def test_legacy_sha256_hash_accepted_and_upgraded():
    creds = InMemoryCredentialStore()
    service = AuthService(InMemoryUserStore(), InMemorySessionStore(), creds, InMemoryAuditStore())
    legacy = hashlib.sha256(("salt" + "oldpass").encode()).hexdigest()
    await creds.set_password("u1", legacy, "salt")
    assert await service.verify_password("u1", "wrong") is False
    assert await service.verify_password("u1", "oldpass") is True
    upgraded = await creds.get_credentials("u1")
    assert upgraded.password_hash.startswith("pbkdf2$100000$")
    assert await service.verify_password("u1", "oldpass") is True


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


async def test_user_store_returns_copies():
    store = InMemoryUserStore()
    user = await store.create_user("alice", "a@x.io", ["users"])
    user.groups.append("admin")
    user.disabled = True
    fresh = await store.get_user(user.id)
    assert fresh.groups == ["users"] and fresh.disabled is False


async def test_user_store_lookup_and_update():
    store = InMemoryUserStore()
    user = await store.create_user("Alice", "Alice@X.com")
    assert (await store.get_user("alice")).id == user.id
    assert (await store.get_user_by_email("alice@x.com")).id == user.id
    with pytest.raises(ValueError):
        await store.create_user("ALICE", "other@x.io")
    updated = await store.update_user(user.id, last_login_at=123, home_dir="/h")
    assert updated.last_login_at == 123 and updated.home_dir == "/h"
    with pytest.raises(KeyError):
        await store.update_user("missing", name="x")


async def test_group_store_case_insensitive():
    store = InMemoryGroupStore()
    group = await store.create_group("Editors", "d", ["docs:*"])
    assert (await store.get_group("editors")).id == group.id
    with pytest.raises(ValueError):
        await store.create_group("EDITORS")
    updated = await store.update_group("EDITORS", permissions=["x:y"])
    assert updated.name == "Editors" and updated.permissions == ["x:y"]
    await store.delete_group("editors")
    assert await store.get_group("Editors") is None


async def test_session_store_cleanup():
    store = InMemorySessionStore()
    s = await store.create_session("u1", "tok", SessionMetadata(), user_name="alice")
    assert s.user_name == "alice"
    store._sessions["tok"].last_activity -= 10_000
    await store.cleanup(5_000)
    assert await store.get_session("tok") is None


async def test_audit_store_cap_and_filters():
    store = InMemoryAuditStore()
    for i in range(1005):
        await store.log(AuditEntry(event="login" if i % 2 else "logout", username=f"u{i % 3}"))
    entries = await store.get_entries()
    assert len(entries) == 1000
    assert len({e.id for e in entries}) == 1000
    logins = await store.get_entries(AuditQueryOptions(event="LOGIN", username="U1", limit=5))
    assert len(logins) == 5
    assert all(e.event == "login" and e.username == "u1" for e in logins)


# ---------------------------------------------------------------------------
# Auth service
# ---------------------------------------------------------------------------


def _service(**opts) -> AuthService:
    return AuthService(
        InMemoryUserStore(), InMemorySessionStore(), InMemoryCredentialStore(),
        InMemoryAuditStore(), InMemoryGroupStore(),
        options=AuthServiceOptions(**opts),
    )


async def test_seed_is_idempotent():
    service = _service()
    await service.seed("pw12")
    await service.seed("pw12")
    assert [u.name for u in await service.user_store.get_users()] == ["root"]
    groups = {g.name: g for g in await service.group_store.get_groups()}
    assert groups["admin"].permissions == ["*"] and groups["admin"].built_in
    assert groups["users"].permissions == ["commands:execute"] and groups["users"].built_in


async def test_login_and_lockout():
    service = _service(max_login_attempts=3, lockout_ms=60_000)
    await service.seed("pw12")
    result = await service.login("root", "pw12")
    assert result.permissions == ["*"]
    assert (await service.user_store.get_user("root")).last_login_at is not None
    for _ in range(3):
        with pytest.raises(UsersApiError) as exc:
            await service.login("root", "bad")
        assert exc.value.status_code == 401
    with pytest.raises(UsersApiError) as exc:
        await service.login("root", "pw12")
    assert exc.value.status_code == 429
    assert exc.value.message == "account locked, try again later"


async def test_authenticate_token_resolves_live_user():
    service = _service()
    await service.seed("pw12")
    result = await service.login("root", "pw12")
    await service.group_store.create_group("ops", None, ["ops:run"])
    await service.user_store.update_user(result.user.id, groups=["ops"])
    ctx = await service.authenticate_token(result.token)
    assert ctx.groups == ["ops"] and ctx.permissions == ["ops:run"]
    assert ctx.can("ops:run") and not ctx.can("users:read")


async def test_authenticate_token_idle_expiry():
    service = _service(session_timeout_ms=1_000)
    await service.seed("pw12")
    result = await service.login("root", "pw12")
    service.session_store._sessions[hash_token(result.token)].last_activity -= 5_000
    with pytest.raises(UsersApiError) as exc:
        await service.authenticate_token(result.token)
    assert exc.value.status_code == 401
    assert await service.session_store.get_session(hash_token(result.token)) is None


async def test_revoke_broadcasts_with_async_and_sync_fns():
    received: list[str] = []

    async def async_fn(msg: str) -> None:
        received.append(msg)

    service = _service()
    service.broadcast_fn = async_fn
    await service.seed("pw12")
    result = await service.login("root", "pw12")
    await service.revoke_user_sessions(result.user.id, "test")
    assert json.loads(received[0]) == {
        "type": "session-kicked",
        "data": {"sessionId": result.session.id, "userId": result.user.id, "reason": "test"},
    }

    service.broadcast_fn = lambda msg: received.append(msg)
    result = await service.login("root", "pw12")
    await service.revoke_user_sessions(result.user.id, "again")
    assert json.loads(received[1])["data"]["reason"] == "again"


async def test_broadcast_failure_does_not_break_revoke():
    def boom(msg: str) -> None:
        raise RuntimeError("socket down")

    service = _service()
    service.broadcast_fn = boom
    await service.seed("pw12")
    result = await service.login("root", "pw12")
    assert await service.revoke_user_sessions(result.user.id, "x") == 1
    assert await service.session_store.get_session(hash_token(result.token)) is None


# ---------------------------------------------------------------------------
# Plugin lifecycle
# ---------------------------------------------------------------------------


async def test_startup_seeds_and_runs_cleanup_on_running_loop():
    plugin = (
        CliUsersBuilder()
        .set_default_admin_password("pw12")
        .set_session_timeout(50)
        .set_cleanup_interval(20)
        .build()
    )
    await plugin.startup()
    try:
        assert plugin._cleanup_task is not None and not plugin._cleanup_task.done()
        assert plugin._cleanup_task.get_loop() is asyncio.get_running_loop()
        result = await plugin.auth_service.login("root", "pw12")
        assert await plugin.session_store.get_session(hash_token(result.token)) is not None
        await asyncio.sleep(0.2)
        assert await plugin.session_store.get_session(hash_token(result.token)) is None
    finally:
        await plugin.shutdown()
    assert plugin._cleanup_task is None


async def test_startup_is_idempotent_and_shutdown_safe():
    plugin = CliUsersBuilder().build()
    await plugin.shutdown()  # before startup: no-op
    await plugin.startup()
    task = plugin._cleanup_task
    await plugin.startup()
    assert plugin._cleanup_task is task
    assert len(await plugin.user_store.get_users()) == 1
    await plugin.shutdown()
    assert task.cancelled() or task.done()


async def test_custom_stores_are_used():
    users = InMemoryUserStore()
    plugin = CliUsersBuilder().set_user_store(users).build()
    await plugin.startup()
    try:
        assert plugin.user_store is users
        assert (await users.get_user("root")) is not None
    finally:
        await plugin.shutdown()


async def test_build_users_plugin_is_deprecated_wrapper():
    with pytest.warns(DeprecationWarning):
        plugin = await build_users_plugin(UsersPluginOptions(default_admin_password="pw12"))
    try:
        assert plugin.prefix == "/api/v1/qcli"
        assert plugin._cleanup_task is not None
        assert (await plugin.auth_service.login("root", "pw12")).token
    finally:
        await plugin.shutdown()
