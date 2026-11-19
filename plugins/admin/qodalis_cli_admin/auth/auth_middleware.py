"""FastAPI dependency for JWT authentication."""

from __future__ import annotations

from typing import Any

import jwt
from fastapi import Request, HTTPException

from .jwt_service import JwtService

#: The users plugin's refusal of ``host`` API tokens (same text).
HOST_TOKENS_CANNOT_USE_CLI_ROUTES = "forbidden: host tokens cannot use CLI routes"


def create_users_auth_dependency(users_plugin: Any):
    """Admin auth through the users plugin: a valid users session holding
    ``admin:access`` (admin ``*`` has it).  ``host`` API tokens are refused
    (403), also when the users plugin does not protect the server."""

    async def require_auth(request: Request) -> dict[str, Any]:
        auth_service = users_plugin.auth_service
        if auth_service.external_auth:
            # The host application signs users in: its external auth adapter
            # authenticates the request (e.g. the bearer token handed to the
            # dashboard through /qcli/admin/#token=…).
            # The users plugin's CLI variant refuses host tokens itself; a
            # users plugin released before host tokens has only require_auth.
            authenticate = getattr(users_plugin, "require_cli_auth", None) or users_plugin.require_auth
            ctx = await authenticate(request)
        else:
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
            await users_plugin.ensure_seeded()
            ctx = await auth_service.authenticate_token(auth_header[7:].strip())
        if getattr(ctx, "is_host_token", False):
            raise HTTPException(status_code=403, detail=HOST_TOKENS_CANNOT_USE_CLI_ROUTES)
        if ctx.must_change_password:
            raise HTTPException(status_code=403, detail="password change required")
        if not ctx.can("admin:access"):
            raise HTTPException(status_code=403, detail="missing permission: admin:access")
        return {
            "username": ctx.user.name,
            "authenticatedAt": (ctx.session.created_at // 1000) if ctx.session else None,
        }

    return require_auth


def create_auth_dependency(jwt_service: JwtService):
    """Return a FastAPI ``Depends`` callable that validates Bearer tokens."""

    async def require_auth(request: Request) -> dict[str, Any]:
        """Extract and verify the JWT Bearer token from the Authorization header.

        Returns the decoded payload on success; raises ``HTTPException(401)``
        otherwise.
        """
        auth_header = request.headers.get("Authorization", "")

        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

        token = auth_header[7:]

        try:
            payload = jwt_service.verify_token(token)
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

        return payload

    return require_auth


async def require_auth(request: Request) -> dict[str, Any]:  # noqa: F811
    """No-op fallback; use ``create_auth_dependency`` to create a real dependency."""
    raise HTTPException(status_code=401, detail="Auth not configured")
