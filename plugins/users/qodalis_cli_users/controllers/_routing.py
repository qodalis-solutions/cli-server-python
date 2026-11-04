"""Routing helpers shared by the users controllers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from fastapi import APIRouter, Depends

from ..errors import UsersApiError, UsersApiRoute

if TYPE_CHECKING:
    from ..services.auth_service import AuthService

USERS_READ_ONLY = "Users are managed by the host application"
GROUPS_READ_ONLY = "Groups are managed by the host application"


def new_router(prefix: str, tag: str) -> APIRouter:
    return APIRouter(prefix=prefix, tags=[tag], route_class=UsersApiRoute)


def collection(router: APIRouter, method: str, **kwargs: Any) -> Callable[[Callable], Callable]:
    """Register a collection endpoint at both ``<prefix>`` and ``<prefix>/``
    so neither form redirects."""

    def decorator(func: Callable) -> Callable:
        router.add_api_route("", func, methods=[method], **kwargs)
        router.add_api_route("/", func, methods=[method], include_in_schema=False, **kwargs)
        return func

    return decorator


def store_write(auth_service: AuthService, *, users: bool = False, groups: bool = False) -> list:
    """Route ``dependencies`` for an endpoint writing to the user and/or group
    store: 501 when that store is read-only (``read_only_users`` /
    ``read_only_groups``, read on every request), before authentication,
    body validation and any store call."""

    async def guard() -> None:
        options = auth_service.options
        if users and options.read_only_users:
            raise UsersApiError(501, USERS_READ_ONLY)
        if groups and options.read_only_groups:
            raise UsersApiError(501, GROUPS_READ_ONLY)

    return [Depends(guard)]
