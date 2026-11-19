"""FastAPI dependencies for authenticating users-plugin (and host) requests.

``get_auth`` resolves the caller (bearer token, or the external auth adapter)
to an :class:`AuthContext` holding the **live** user and its effective
permissions, and enforces CSRF (B9) and the ``mustChangePassword`` gate (B4).
``get_auth_pending`` is the same without the password-change gate (for the
few endpoints allowed while a password change is pending).  ``get_cli_auth``
also refuses ``host`` API tokens (403): the users plugin's own routes use it,
while ``get_auth`` (``require_auth`` / ``require_permission``) guards the
host application's routes and accepts them.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import Depends, Request

from .._http import SAFE_METHODS, bearer_token, get_scope_auth, has_bearer, origin_allowed
from ..errors import forbidden, unauthorized
from ..services.auth_service import HOST_TOKENS_CANNOT_USE_CLI_ROUTES, AuthContext, AuthService

GetAuth = Callable[[Request], Awaitable[AuthContext]]

ORIGIN_NOT_ALLOWED = "origin not allowed"


async def authenticate_request(
    auth_service: AuthService, request: Request, *, touch_api_token: bool = True,
) -> AuthContext:
    """Authenticate an HTTP request (no password-change gate).
    ``touch_api_token=False`` leaves an API token's ``last_used_at`` for the
    caller to record."""
    if auth_service.external_auth:
        # Ambient (adapter/cookie) credentials: state-changing requests
        # without a bearer token must come from an allowed Origin (CSRF);
        # same-origin requests (e.g. the bundled dashboard) always pass.
        if request.method.upper() not in SAFE_METHODS and not has_bearer(request.headers):
            if not origin_allowed(
                request.headers.get("origin"),
                auth_service.options.allowed_origins,
                request.scope,
                auth_service.options.trust_proxy,
            ):
                raise forbidden(ORIGIN_NOT_ALLOWED)
        return await auth_service.authenticate_external(request, touch_api_token=touch_api_token)
    token = bearer_token(request.headers)
    if not token:
        raise unauthorized()
    return await auth_service.authenticate_token(token, touch_api_token=touch_api_token)


def ensure_cli_audience(ctx: AuthContext) -> None:
    """403 for a ``host`` API token on a CLI route."""
    if ctx.is_host_token:
        raise forbidden(HOST_TOKENS_CANNOT_USE_CLI_ROUTES)


def cli_only(get: GetAuth) -> GetAuth:
    """``get`` that also refuses ``host`` API tokens."""

    async def dependency(request: Request) -> AuthContext:
        ctx = await get(request)
        ensure_cli_audience(ctx)
        return ctx

    return dependency


def permission_dependency(get: GetAuth, permission: str) -> Callable[..., Awaitable[AuthContext]]:
    """FastAPI dependency: ``get``'s caller holding ``permission`` (403)."""

    async def dependency(ctx: AuthContext = Depends(get)) -> AuthContext:
        if not ctx.can(permission):
            raise forbidden(f"missing permission: {permission}")
        return ctx

    return dependency


def create_auth_dependencies(
    auth_service: AuthService,
    ensure_ready: Callable[[], Awaitable[None]] | None = None,
) -> tuple[GetAuth, Callable[[str], Callable[..., Awaitable[AuthContext]]], GetAuth, GetAuth]:
    """Return ``(get_auth, require_permission, get_auth_pending,
    get_cli_auth)``."""

    async def get_auth_pending(request: Request) -> AuthContext:
        if ensure_ready is not None:
            await ensure_ready()
        ctx = get_scope_auth(request.scope)
        if isinstance(ctx, AuthContext):
            if ctx.last_used_pending and ctx.api_token is not None:
                ctx.last_used_pending = False
                await auth_service.touch_api_token(ctx.api_token)
            return ctx
        return await authenticate_request(auth_service, request)

    async def get_auth(request: Request) -> AuthContext:
        ctx = await get_auth_pending(request)
        auth_service.ensure_password_changed(ctx)
        return ctx

    async def get_cli_auth(request: Request) -> AuthContext:
        ctx = await get_auth_pending(request)
        # Before the password-change gate: a host token gets this 403 whatever
        # the user's state.
        ensure_cli_audience(ctx)
        auth_service.ensure_password_changed(ctx)
        return ctx

    def require_permission(permission: str) -> Callable[..., Awaitable[AuthContext]]:
        return permission_dependency(get_auth, permission)

    return get_auth, require_permission, get_auth_pending, get_cli_auth


def bearer_token_from(request: Request) -> str | None:  # backwards compat
    return bearer_token(request.headers)
