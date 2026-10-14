"""Routing helpers shared by the users controllers."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter

from ..errors import UsersApiRoute


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
