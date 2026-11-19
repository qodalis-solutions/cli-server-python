"""Permission catalog controller — ``/permissions``.

- ``GET /permissions``           the catalog entries the caller holds
- ``GET /permissions?all=true``  every catalog entry (``users:read``)

The dashboard's and console's token pickers are built from it.
"""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends

from ..errors import bad_request, forbidden
from ..permissions import USERS_READ
from ..services.auth_service import AuthContext, AuthService
from ._routing import collection, new_router


def create_permissions_router(auth_service: AuthService, get_auth: Callable) -> APIRouter:
    router = new_router("/permissions", "permissions")

    @collection(router, "GET")
    async def list_permissions(all: str | None = None, ctx: AuthContext = Depends(get_auth)):  # noqa: A002
        if all not in (None, "true", "false"):
            raise bad_request("all must be true or false")
        entries = auth_service.permission_catalog.all()
        if all == "true":
            if not ctx.can(USERS_READ):
                raise forbidden(f"permission denied: {USERS_READ}")
        else:
            # Wildcards in the caller's (scope-narrowed) permissions match.
            entries = [p for p in entries if ctx.can(p.name)]
        return [p.to_dict() for p in entries]

    return router
