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

from typing import Any, Awaitable, Callable

from fastapi import Depends, HTTPException, Request

from .._http import SAFE_METHODS, bearer_token, get_scope_auth, has_bearer, origin_allowed
from ..errors import QcliGuardError, UsersApiError, forbidden, unauthorized
from ..services.auth_service import (
    HOST_TOKEN_REQUIRED,
    HOST_TOKENS_CANNOT_USE_CLI_ROUTES,
    AuthContext,
    AuthService,
)

GetAuth = Callable[[Request], Awaitable[AuthContext]]

ORIGIN_NOT_ALLOWED = "origin not allowed"
#: ``request.state`` key of the request's authenticated context.
AUTH_MEMO_KEY = "qcli_auth_memo"


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


def tag_guard(guard: Any, permission: str | None, host_token_only: bool) -> Any:
    """Mark a guard dependency with what it requires (read by the public
    OpenAPI document: ``x-required-permission`` / ``x-host-token-only``)."""
    guard.qcli_permission = permission
    guard.qcli_host_token_only = host_token_only
    return guard


def set_caller(request: Request, ctx: AuthContext) -> None:
    """Expose the caller to the host's handler (``request.state.qcli_caller``)."""
    request.state.qcli_caller = ctx.caller


def permission_dependency(get: GetAuth, permission: str) -> Callable[..., Awaitable[AuthContext]]:
    """FastAPI dependency: ``get``'s caller holding ``permission`` (403)."""

    async def dependency(request: Request, ctx: AuthContext = Depends(get)) -> AuthContext:
        if not ctx.can(permission):
            raise forbidden(f"missing permission: {permission}")
        set_caller(request, ctx)
        return ctx

    return tag_guard(dependency, permission, False)


async def authenticate_host_token(get: GetAuth, request: Request) -> AuthContext:
    """``get``'s caller when it is a ``host`` API token.  Every refusal is a
    :class:`QcliGuardError` (``{"error": …}`` on host routes): the
    authentication texts as ``get`` answers them, then 403
    ``forbidden: a host token is required`` for a session, an adapter
    identity or a ``cli`` token."""
    try:
        ctx = await get(request)
    except QcliGuardError:
        raise
    except UsersApiError as exc:
        raise QcliGuardError(exc.status_code, exc.message) from None
    except HTTPException as exc:
        # Raised by an external auth adapter: keep its status and detail.
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        raise QcliGuardError(exc.status_code, detail, getattr(exc, "headers", None)) from None
    if not ctx.is_host_token:
        raise QcliGuardError(403, HOST_TOKEN_REQUIRED)
    return ctx


def host_token_dependency(get: GetAuth, permission: str | None) -> Callable[..., Awaitable[AuthContext]]:
    """FastAPI dependency: a ``host`` API token (C2), holding ``permission``
    when given (403 ``missing permission: <permission>``, checked after the
    audience)."""

    async def dependency(request: Request) -> AuthContext:
        ctx = await authenticate_host_token(get, request)
        if permission and not ctx.can(permission):
            raise QcliGuardError(403, f"missing permission: {permission}")
        set_caller(request, ctx)
        return ctx

    return tag_guard(dependency, permission, True)


def create_auth_dependencies(
    auth_service: AuthService,
    ensure_ready: Callable[[], Awaitable[None]] | None = None,
) -> tuple[GetAuth, Callable[[str], Callable[..., Awaitable[AuthContext]]], GetAuth, GetAuth]:
    """Return ``(get_auth, require_permission, get_auth_pending,
    get_cli_auth)``."""

    async def get_auth_pending(request: Request) -> AuthContext:
        if ensure_ready is not None:
            await ensure_ready()
        # One authentication per request (one token look-up, one touch),
        # however many guards run on the route.
        memo = getattr(request.state, AUTH_MEMO_KEY, None)
        if isinstance(memo, AuthContext):
            return memo
        ctx = get_scope_auth(request.scope)
        if isinstance(ctx, AuthContext):
            if ctx.last_used_pending and ctx.api_token is not None:
                ctx.last_used_pending = False
                await auth_service.touch_api_token(ctx.api_token)
        else:
            ctx = await authenticate_request(auth_service, request)
        setattr(request.state, AUTH_MEMO_KEY, ctx)
        return ctx

    async def authenticate(request: Request) -> AuthContext:
        """``get_auth`` without exposing the caller: the guards built on it
        set ``request.state.qcli_caller`` only once they fully passed."""
        ctx = await get_auth_pending(request)
        auth_service.ensure_password_changed(ctx)
        return ctx

    async def get_auth(request: Request) -> AuthContext:
        ctx = await authenticate(request)
        set_caller(request, ctx)
        return ctx

    tag_guard(get_auth, None, False)
    #: For guards built on it (``require_permission``, ``require_host_token``,
    #: the public-api guard).
    get_auth.authenticate = authenticate  # type: ignore[attr-defined]

    async def get_cli_auth(request: Request) -> AuthContext:
        ctx = await get_auth_pending(request)
        # Before the password-change gate: a host token gets this 403 whatever
        # the user's state.
        ensure_cli_audience(ctx)
        auth_service.ensure_password_changed(ctx)
        return ctx

    def require_permission(permission: str) -> Callable[..., Awaitable[AuthContext]]:
        return permission_dependency(authenticate, permission)

    return get_auth, require_permission, get_auth_pending, get_cli_auth


def bearer_token_from(request: Request) -> str | None:  # backwards compat
    return bearer_token(request.headers)
