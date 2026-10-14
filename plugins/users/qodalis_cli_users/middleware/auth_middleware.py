"""FastAPI dependencies for authenticating users-plugin (and host) requests.

``get_auth`` resolves the caller (bearer token, or the external auth adapter)
to an :class:`AuthContext` holding the **live** user and its effective
permissions, and enforces CSRF (B9) and the ``mustChangePassword`` gate (B4).
``get_auth_pending`` is the same without the password-change gate (for the
few endpoints allowed while a password change is pending).
"""

from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import Depends, Request

from .._http import SAFE_METHODS, bearer_token, get_scope_auth, has_bearer, origin_allowed
from ..errors import forbidden, unauthorized
from ..services.auth_service import AuthContext, AuthService

GetAuth = Callable[[Request], Awaitable[AuthContext]]

ORIGIN_NOT_ALLOWED = "origin not allowed"


async def authenticate_request(auth_service: AuthService, request: Request) -> AuthContext:
    """Authenticate an HTTP request (no password-change gate)."""
    if auth_service.external_auth:
        # Ambient (adapter/cookie) credentials: state-changing requests
        # without a bearer token must come from an allowed Origin (CSRF).
        if request.method.upper() not in SAFE_METHODS and not has_bearer(request.headers):
            if not origin_allowed(request.headers.get("origin"), auth_service.options.allowed_origins):
                raise forbidden(ORIGIN_NOT_ALLOWED)
        return await auth_service.authenticate_external(request)
    token = bearer_token(request.headers)
    if not token:
        raise unauthorized()
    return await auth_service.authenticate_token(token)


def create_auth_dependencies(
    auth_service: AuthService,
    ensure_ready: Callable[[], Awaitable[None]] | None = None,
) -> tuple[GetAuth, Callable[[str], Callable[..., Awaitable[AuthContext]]], GetAuth]:
    """Return ``(get_auth, require_permission, get_auth_pending)``."""

    async def get_auth_pending(request: Request) -> AuthContext:
        if ensure_ready is not None:
            await ensure_ready()
        ctx = get_scope_auth(request.scope)
        if isinstance(ctx, AuthContext):
            return ctx
        return await authenticate_request(auth_service, request)

    async def get_auth(request: Request) -> AuthContext:
        ctx = await get_auth_pending(request)
        auth_service.ensure_password_changed(ctx)
        return ctx

    def require_permission(permission: str) -> Callable[..., Awaitable[AuthContext]]:
        async def dependency(ctx: AuthContext = Depends(get_auth)) -> AuthContext:
            if not ctx.can(permission):
                raise forbidden(f"missing permission: {permission}")
            return ctx

        return dependency

    return get_auth, require_permission, get_auth_pending


def bearer_token_from(request: Request) -> str | None:  # backwards compat
    return bearer_token(request.headers)
