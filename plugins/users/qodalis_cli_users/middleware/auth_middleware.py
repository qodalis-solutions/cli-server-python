"""FastAPI dependencies for authenticating users-plugin requests.

``get_auth`` resolves the caller (bearer token, or the external auth adapter)
to an :class:`AuthContext` holding the **live** user and its effective
permissions.  ``require_permission`` additionally enforces a permission.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import Depends, Request

from ..errors import forbidden, unauthorized
from ..services.auth_service import AuthContext, AuthService

GetAuth = Callable[[Request], Awaitable[AuthContext]]


def bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() != "bearer ":
        return None
    token = auth[7:].strip()
    return token or None


def create_auth_dependencies(
    auth_service: AuthService,
    ensure_ready: Callable[[], Awaitable[None]] | None = None,
) -> tuple[GetAuth, Callable[[str], Callable[..., Awaitable[AuthContext]]]]:
    """Return ``(get_auth, require_permission)`` dependency factories."""

    async def get_auth(request: Request) -> AuthContext:
        if ensure_ready is not None:
            await ensure_ready()
        if auth_service.external_auth:
            return await auth_service.authenticate_external(request)
        token = bearer_token(request)
        if not token:
            raise unauthorized()
        return await auth_service.authenticate_token(token)

    def require_permission(permission: str) -> Callable[..., Awaitable[AuthContext]]:
        async def dependency(ctx: AuthContext = Depends(get_auth)) -> AuthContext:
            if not ctx.can(permission):
                raise forbidden(f"missing permission: {permission}")
            return ctx

        return dependency

    return get_auth, require_permission
