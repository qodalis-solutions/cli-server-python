"""Audit controller — /audit routes."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, Response

from ..models import AuditQueryOptions, Session
from ..stores.interfaces import IAuditStore


def create_audit_router(
    audit_store: IAuditStore,
    require_admin: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/audit", tags=["audit"])

    # GET / — list audit entries (admin only)
    @router.get("/")
    async def list_entries(
        event: str | None = None,
        username: str | None = None,
        limit: int | None = None,
        session: Session = Depends(require_admin),
    ):
        options = AuditQueryOptions(event=event, username=username, limit=limit)
        entries = await audit_store.get_entries(options)
        return [e.to_dict() for e in entries]

    # DELETE / — clear all audit entries (admin only)
    @router.delete("/", status_code=204)
    async def clear_entries(session: Session = Depends(require_admin)):
        await audit_store.clear()
        return Response(status_code=204)

    return router
