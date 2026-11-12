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
from ..models import ApiToken, CreateApiTokenRequest, now_ms
from ..permissions import TOKENS_READ, TOKENS_REVOKE
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


def _validate_name(raw: Any) -> str:
    if not isinstance(raw, str) or not TOKEN_NAME_RE.match(raw):
        raise bad_request("name must be 1-64 characters of [A-Za-z0-9._ -]")
    return raw


def _expires_at(body: CreateApiTokenRequest, allow_never: bool, now: int) -> int | None:
    if "expiresInDays" not in body.model_fields_set:
        return now + DEFAULT_EXPIRES_IN_DAYS * DAY_MS
    days = body.expiresInDays
    if days is None:
        if not allow_never:
            raise bad_request("non-expiring tokens are not allowed")
        return None
    if isinstance(days, float) and days.is_integer():
        days = int(days)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_EXPIRES_IN_DAYS:
        raise bad_request(f"expiresInDays must be an integer between 1 and {MAX_EXPIRES_IN_DAYS}")
    return now + days * DAY_MS


def scope_permissions(ctx: AuthContext, raw: Any) -> list[str] | None:
    """``None`` (omitted/null) = unscoped; ``[]`` = an empty scope.  Field
    errors (400) are raised before the coverage check (403)."""
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(p, str) and p for p in raw):
        raise bad_request("permissions must be an array of strings")
    perms = validate_permissions(raw) or []
    for perm in perms:
        if not ctx.can(perm):
            raise forbidden(f"forbidden: cannot grant {sanitize(perm, 128)}")
    return perms


def ensure_can_create_tokens(ctx: AuthContext, allow_external: bool = False) -> None:
    """Only a signed-in session creates tokens: 403 for API tokens (a leaked
    token must not mint more), 501 for external auth (no sessions) unless the
    host allows API tokens next to its sign-in (``allow_external``,
    ``api_tokens_with_external_auth``)."""
    if ctx.api_token is not None:
        raise forbidden(API_TOKENS_CANNOT_CREATE)
    if ctx.session is None or (ctx.external and not allow_external):
        raise UsersApiError(501, MANAGED_BY_HOST)


async def revoke_api_token(auth_service: AuthService, ctx: AuthContext, record: ApiToken) -> None:
    """Delete an API token as ``ctx`` (``DELETE /tokens/{id}`` and link
    approvals the terminal can no longer collect); audited
    (``token_revoked``) and published (``token:revoked``)."""
    await auth_service.api_token_store.delete_token(record.id)
    owner = await auth_service.user_store.get_user(record.user_id)
    # The audit also notifies change listeners: sockets opened with this
    # token are re-checked and closed (4401 "session ended").
    await auth_service.audit(
        "token_revoked", owner.name if owner else record.user_id,
        details=f"{record.name} ({record.id})",
        actor=ctx.user.name, actor_id=ctx.user.id, user_id=record.user_id,
        metadata={"tokenId": record.id, "tokenName": record.name},
    )
    await auth_service.publish("token:revoked", {
        "tokenId": record.id, "name": record.name, "userId": record.user_id, "actor": ctx.user.name,
    }, permission=TOKENS_READ, user_id=record.user_id)


def default_token_expiry(now: int | None = None) -> int:
    """Expiry of a token created without ``expiresInDays`` (90 days)."""
    return (now if now is not None else now_ms()) + DEFAULT_EXPIRES_IN_DAYS * DAY_MS


async def create_api_token(
    auth_service: AuthService,
    ctx: AuthContext,
    name: str,
    permissions: list[str] | None,
    expires_at: int | None,
) -> tuple[str, ApiToken]:
    """Create an API token for the caller (``POST /tokens`` and link
    approvals): the name is unique per user (409), at most
    ``MAX_TOKENS_PER_USER`` unexpired tokens (400); audited
    (``token_created``) and published (``token:created``).  The caller
    validated the session, the name, the expiry and the scope.  Returns the
    raw token (shown once) and the stored record."""
    store = auth_service.api_token_store
    now = now_ms()
    existing = await store.get_tokens(ctx.user.id)
    if any(t.name.lower() == name.lower() for t in existing):
        raise conflict(f"token '{name}' already exists")
    # Expired tokens stay listed but do not count toward the limit.
    if sum(1 for t in existing if not t.is_expired(now)) >= MAX_TOKENS_PER_USER:
        raise bad_request(f"a user may have at most {MAX_TOKENS_PER_USER} tokens")
    token = auth_service.generate_api_token()
    record = await store.create_token(ApiToken(
        id=uuid.uuid4().hex,
        user_id=ctx.user.id,
        name=name,
        token_hash=hash_token(token),
        created_at=now,
        expires_at=expires_at,
        permissions=permissions,
    ))
    await auth_service.audit(
        "token_created", ctx.user.name,
        details=f"{record.name} ({record.id})",
        actor=ctx.user.name, actor_id=ctx.user.id, user_id=ctx.user.id,
        metadata={"tokenId": record.id, "tokenName": record.name},
    )
    await auth_service.publish("token:created", {
        "tokenId": record.id, "name": record.name, "userId": record.user_id, "actor": ctx.user.name,
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
        name = _validate_name(body.name)
        expires_at = _expires_at(body, auth_service.options.allow_non_expiring_tokens, now_ms())
        permissions = scope_permissions(ctx, body.permissions)
        token, record = await create_api_token(auth_service, ctx, name, permissions, expires_at)
        return {"token": token, "apiToken": record.to_view(ctx.user.name)}

    @router.delete("/{token_id}", status_code=204)
    async def revoke_token(token_id: str, ctx: AuthContext = Depends(get_auth)):
        record = await store.get_token(token_id)
        if record is None or (record.user_id != ctx.user.id and not ctx.can(TOKENS_REVOKE)):
            raise not_found("token not found")
        await revoke_api_token(auth_service, ctx, record)
        return Response(status_code=204)

    return router
