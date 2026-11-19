"""API tokens controller — ``/tokens``.

- ``GET    /tokens``           own tokens (authenticated)
- ``GET    /tokens?all=true``  every user's tokens (``tokens:read``)
- ``POST   /tokens``           create a token (authenticated, session only)
- ``DELETE /tokens/{id}``      revoke (owner, or ``tokens:revoke``)
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends, Response

from ..errors import UsersApiError, bad_request, conflict, forbidden, not_found
from ..models import ApiToken, CreateApiTokenRequest, User, now_ms
from ..permissions import TOKENS_READ, TOKENS_REVOKE, has_permission
from ..services.auth_service import AuthContext, AuthService, hash_token
from ..validation import sanitize, validate_permissions
from ._routing import collection, new_router

TOKEN_NAME_RE = re.compile(r"^[A-Za-z0-9._ -]{1,64}$")
TOKEN_NAME_MAX = 64
MAX_TOKENS_PER_USER = 20
DEFAULT_EXPIRES_IN_DAYS = 90
MAX_EXPIRES_IN_DAYS = 365
DAY_MS = 24 * 60 * 60 * 1000

API_TOKENS_CANNOT_CREATE = "forbidden: API tokens cannot create tokens"
MANAGED_BY_HOST = "Authentication is managed by the host application"

#: ``cli`` tokens use the CLI routes; ``host`` tokens only the host
#: application's guarded routes (created through ``users_plugin.api_tokens``).
API_TOKEN_AUDIENCES = ("cli", "host")
HOST_TOKENS_CREATED_BY_HOST = "host tokens can only be created by the host application"
INVALID_AUDIENCE = "audience must be cli or host"
HOST_TOKENS_NEED_PERMISSIONS = "host tokens need permissions"
NOT_A_HOST_PERMISSION = "not a host permission: "
EXPIRY_CONFLICT = "expiresInDays and neverExpires cannot both be set"
NON_EXPIRING_NOT_ALLOWED = "non-expiring tokens are not allowed"


def _http_audience(body: CreateApiTokenRequest) -> str:
    """``POST /tokens`` creates ``cli`` tokens only (absent = ``cli``)."""
    if "audience" not in body.model_fields_set:
        return "cli"
    audience = body.audience
    if not isinstance(audience, str) or audience not in API_TOKEN_AUDIENCES:
        raise bad_request(INVALID_AUDIENCE)
    if audience == "host":
        raise bad_request(HOST_TOKENS_CREATED_BY_HOST)
    return audience


def validate_token_name(raw: Any) -> str:
    if not isinstance(raw, str) or not TOKEN_NAME_RE.match(raw):
        raise bad_request("name must be 1-64 characters of [A-Za-z0-9._ -]")
    return raw


def _expires_at(body: CreateApiTokenRequest, allow_never: bool, now: int) -> int | None:
    if "expiresInDays" not in body.model_fields_set:
        return now + DEFAULT_EXPIRES_IN_DAYS * DAY_MS
    return token_expiry(body.expiresInDays, allow_never, now)


def token_expiry(days: Any, allow_never: bool, now: int) -> int | None:
    """Expiry of a token valid ``days`` days (1-365); ``None`` = never
    expires (only when allowed)."""
    if days is None:
        if not allow_never:
            raise bad_request(NON_EXPIRING_NOT_ALLOWED)
        return None
    if isinstance(days, float) and days.is_integer():
        days = int(days)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_EXPIRES_IN_DAYS:
        raise bad_request(f"expiresInDays must be an integer between 1 and {MAX_EXPIRES_IN_DAYS}")
    return now + days * DAY_MS


def parse_scope(raw: Any) -> list[str] | None:
    """Syntax of a token scope: ``None`` (omitted/null) = unscoped; ``[]`` =
    an empty scope (400 for anything but an array of valid permissions)."""
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(p, str) and p for p in raw):
        raise bad_request("permissions must be an array of strings")
    return validate_permissions(raw) or []


def ensure_grantable(granted: list[str], permissions: list[str] | None) -> None:
    """403 for the first scope entry ``granted`` (the creator's live
    permissions) does not cover."""
    for perm in permissions or []:
        if not has_permission(granted, perm):
            raise forbidden(f"forbidden: cannot grant {sanitize(perm, 128)}")


def scope_permissions(granted: list[str], raw: Any) -> list[str] | None:
    """Parse a scope and check it is covered by ``granted``.  Field errors
    (400) are raised before the coverage check (403)."""
    perms = parse_scope(raw)
    ensure_grantable(granted, perms)
    return perms


def host_scope(catalog: Any, permissions: list[str] | None) -> list[str]:
    """A ``host`` token's scope: required, and every entry a ``host``-surface
    catalog entry (case-insensitive, wildcards never match), returned in the
    catalog's spelling."""
    if not permissions:
        raise bad_request(HOST_TOKENS_NEED_PERMISSIONS)
    out: list[str] = []
    for perm in permissions:
        entry = None if "*" in perm else catalog.find(perm)
        if entry is None or getattr(entry, "surface", None) != "host":
            raise bad_request(NOT_A_HOST_PERMISSION + sanitize(perm, 128))
        if entry.name not in out:
            out.append(entry.name)
    return out


def ensure_can_create_tokens(ctx: AuthContext, allow_external: bool = False) -> None:
    """Only a signed-in session creates tokens: 403 for API tokens (a leaked
    token must not mint more), 501 for external auth (no sessions) unless the
    host allows API tokens next to its sign-in (``allow_external``,
    ``api_tokens_with_external_auth``)."""
    if ctx.api_token is not None:
        raise forbidden(API_TOKENS_CANNOT_CREATE)
    if ctx.session is None or (ctx.external and not allow_external):
        raise UsersApiError(501, MANAGED_BY_HOST)


async def revoke_api_token(
    auth_service: AuthService, actor: tuple[str, str | None], record: ApiToken,
) -> None:
    """Delete an API token as ``actor`` (``(name, id)``): ``DELETE
    /tokens/{id}``, link approvals the terminal can no longer collect and
    ``users_plugin.api_tokens.revoke``; audited (``token_revoked``) and
    published (``token:revoked``)."""
    actor_name, actor_id = actor
    await auth_service.api_token_store.delete_token(record.id)
    owner = await auth_service.user_store.get_user(record.user_id)
    # The audit also notifies change listeners: sockets opened with this
    # token are re-checked and closed (4401 "session ended").
    await auth_service.audit(
        "token_revoked", owner.name if owner else record.user_id,
        details=f"{record.name} ({record.id})",
        actor=actor_name, actor_id=actor_id, user_id=record.user_id,
        metadata={"tokenId": record.id, "tokenName": record.name},
    )
    await auth_service.publish("token:revoked", {
        "tokenId": record.id, "name": record.name, "userId": record.user_id, "actor": actor_name,
    }, permission=TOKENS_READ, user_id=record.user_id)


def default_token_expiry(now: int | None = None) -> int:
    """Expiry of a token created without ``expiresInDays`` (90 days)."""
    return (now if now is not None else now_ms()) + DEFAULT_EXPIRES_IN_DAYS * DAY_MS


async def create_api_token(
    auth_service: AuthService,
    owner: User,
    actor: tuple[str, str | None],
    name: str,
    permissions: list[str] | None,
    expires_at: int | None,
    audience: str = "cli",
) -> tuple[str, ApiToken]:
    """Create an API token for ``owner`` as ``actor`` (``(name, id)``):
    ``POST /tokens``, link approvals and ``users_plugin.api_tokens.create``.
    The name is unique per user (409), at most ``MAX_TOKENS_PER_USER``
    unexpired tokens of both audiences (400); audited (``token_created``)
    and published (``token:created``).  The caller validated the creator,
    the name, the expiry and the scope.  Returns the raw token (shown once)
    and the stored record."""
    actor_name, actor_id = actor
    store = auth_service.api_token_store
    now = now_ms()
    existing = await store.get_tokens(owner.id)
    if any(t.name.lower() == name.lower() for t in existing):
        raise conflict(f"token '{name}' already exists")
    # Expired tokens stay listed but do not count toward the limit.
    if sum(1 for t in existing if not t.is_expired(now)) >= MAX_TOKENS_PER_USER:
        raise bad_request(f"a user may have at most {MAX_TOKENS_PER_USER} tokens")
    token = auth_service.generate_api_token()
    record = await store.create_token(ApiToken(
        id=uuid.uuid4().hex,
        user_id=owner.id,
        name=name,
        token_hash=hash_token(token),
        created_at=now,
        expires_at=expires_at,
        permissions=permissions,
        audience=audience,
    ))
    await auth_service.audit(
        "token_created", owner.name,
        details=f"{record.name} ({record.id})",
        actor=actor_name, actor_id=actor_id, user_id=owner.id,
        metadata={"tokenId": record.id, "tokenName": record.name},
    )
    await auth_service.publish("token:created", {
        "tokenId": record.id, "name": record.name, "userId": record.user_id, "actor": actor_name,
    }, permission=TOKENS_READ, user_id=record.user_id)
    return token, record


def create_tokens_router(
    auth_service: AuthService,
    get_auth: Callable,
) -> APIRouter:
    router = new_router("/tokens", "tokens")
    store = auth_service.api_token_store

    @collection(router, "GET")
    async def list_tokens(all: str | None = None, ctx: AuthContext = Depends(get_auth)):  # noqa: A002
        if all not in (None, "true", "false"):
            raise bad_request("all must be true or false")
        all = all == "true"  # noqa: A001
        if all and not ctx.can(TOKENS_READ):
            raise forbidden(f"forbidden: missing permission {TOKENS_READ}")
        tokens = await store.get_tokens(None if all else ctx.user.id)
        tokens.sort(key=lambda t: t.created_at)
        return [await auth_service.api_token_view(t) for t in tokens]

    @collection(router, "POST", status_code=201)
    async def create_token(body: CreateApiTokenRequest, ctx: AuthContext = Depends(get_auth)):
        ensure_can_create_tokens(ctx, auth_service.options.api_tokens_with_external_auth)
        _http_audience(body)
        name = validate_token_name(body.name)
        expires_at = _expires_at(body, auth_service.options.allow_non_expiring_tokens, now_ms())
        permissions = scope_permissions(ctx.permissions, body.permissions)
        token, record = await create_api_token(
            auth_service, ctx.user, (ctx.user.name, ctx.user.id), name, permissions, expires_at,
        )
        return {"token": token, "apiToken": record.to_view(ctx.user.name)}

    @router.delete("/{token_id}", status_code=204)
    async def revoke_token(token_id: str, ctx: AuthContext = Depends(get_auth)):
        record = await store.get_token(token_id)
        if record is None or (record.user_id != ctx.user.id and not ctx.can(TOKENS_REVOKE)):
            raise not_found("token not found")
        await revoke_api_token(auth_service, (ctx.user.name, ctx.user.id), record)
        return Response(status_code=204)

    return router
