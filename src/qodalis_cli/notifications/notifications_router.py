"""``/api/v1/qcli/notifications`` (any signed-in user on a protected server; each sees the notifications whose
permission they hold):

- ``GET /?unread=true&severity=&limit=`` → ``{ items, unreadCount }``, newest first;
- ``POST /{id}/read`` → ``{ item, unreadCount }``; ``POST /read-all`` → ``{ marked, unreadCount }``;
- ``DELETE /{id}`` dismisses it for the caller; ``DELETE /{id}?scope=all`` clears it for everyone
  (``notifications:manage``)."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from qodalis_cli_server_abstractions import CLI_NOTIFICATION_SEVERITIES, CLI_NOTIFICATIONS_MANAGE_PERMISSION

from .cli_notification_service import CliNotificationService, CliNotificationViewer

MAX_LIMIT = 500
_NO_STORE = {"Cache-Control": "no-store"}
_NOT_FOUND = {"error": "notification not found"}


def create_notifications_router(
    service: CliNotificationService, viewer_of: Callable[[Request], CliNotificationViewer],
) -> APIRouter:
    router = APIRouter()

    @router.get("")
    @router.get("/")
    async def list_notifications(request: Request) -> JSONResponse:
        q = request.query_params
        unread = q.get("unread") in ("true", "1")
        severity = q.get("severity") or None
        if severity is not None and severity not in CLI_NOTIFICATION_SEVERITIES:
            return JSONResponse({"error": f"invalid severity: {severity} (info, warning, error)"}, status_code=400)
        limit = None
        raw_limit = q.get("limit")
        if raw_limit:
            if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= MAX_LIMIT:
                return JSONResponse({"error": f"limit must be an integer from 1 to {MAX_LIMIT}"}, status_code=400)
            limit = int(raw_limit)
        result = service.list(viewer_of(request), unread=unread, severity=severity, limit=limit)
        return JSONResponse(result, headers=_NO_STORE)

    @router.post("/read-all")
    async def read_all(request: Request) -> JSONResponse:
        viewer = viewer_of(request)
        marked = service.mark_all_read(viewer)
        return JSONResponse({"marked": marked, "unreadCount": service.unread_count(viewer)}, headers=_NO_STORE)

    @router.post("/{notification_id}/read")
    async def read(notification_id: str, request: Request) -> JSONResponse:
        viewer = viewer_of(request)
        item = service.mark_read(viewer, notification_id)
        if item is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        return JSONResponse({"item": item, "unreadCount": service.unread_count(viewer)}, headers=_NO_STORE)

    @router.delete("/{notification_id}")
    async def dismiss(notification_id: str, request: Request) -> JSONResponse:
        viewer = viewer_of(request)
        everyone = request.query_params.get("scope") == "all"
        if everyone and not viewer.can(CLI_NOTIFICATIONS_MANAGE_PERMISSION):
            return JSONResponse(
                {"error": f"forbidden: missing permission {CLI_NOTIFICATIONS_MANAGE_PERMISSION}"}, status_code=403,
            )
        done = service.clear(viewer, notification_id) if everyone else service.dismiss(viewer, notification_id)
        if not done:
            return JSONResponse(_NOT_FOUND, status_code=404)
        return JSONResponse(
            {"cleared" if everyone else "dismissed": True, "unreadCount": service.unread_count(viewer)}, headers=_NO_STORE,
        )

    return router
