"""FastAPI dependency for JWT authentication."""

from __future__ import annotations

from typing import Any

import jwt
from fastapi import Request, HTTPException

from .jwt_service import JwtService


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


# Convenience alias — requires a JwtService instance to be set externally.
# Prefer ``create_auth_dependency(jwt_service)`` for production use.
async def require_auth(request: Request) -> dict[str, Any]:  # noqa: F811
    """Placeholder — use :func:`create_auth_dependency` to create a real dependency."""
    raise HTTPException(status_code=401, detail="Auth not configured")
