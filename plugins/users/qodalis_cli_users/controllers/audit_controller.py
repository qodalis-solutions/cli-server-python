"""Audit controller — ``/audit``."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, Query, Response

from ..models import AuditQueryOptions
from ..permissions import AUDIT_CLEAR, AUDIT_READ
from ..services.auth_service import AuthContext, AuthService
from ._routing import collection, new_router


def create_audit_router(
    auth_service: AuthService,
    require_permission: Callable[[str], Callable],
) -> APIRouter:
    router = new_router("/audit", "audit")
    audit_store = auth_service.audit_store

    @collection(router, "GET")
    async def list_entries(
        event: str | None = None,
        username: str | None = None,
        limit: int | None = Query(default=None, ge=0),
        ctx: AuthContext = Depends(require_permission(AUDIT_READ)),
    ):
        entries = await audit_store.get_entries(
            AuditQueryOptions(event=event or None, username=username or None, limit=limit or None),
        )
        return [e.to_dict() for e in entries]

    @collection(router, "DELETE", status_code=204)
    async def clear_entries(ctx: AuthContext = Depends(require_permission(AUDIT_CLEAR))):
        await audit_store.clear()
        await auth_service.audit("audit_cleared", ctx.user.name, actor=ctx.user.name)
        return Response(status_code=204)

    return router
