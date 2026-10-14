"""Groups controller — ``/groups`` (CRUD, permissions, membership)."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, Response

from ..errors import bad_request, forbidden, not_found
from ..models import CreateGroupRequest, Group, UpdateGroupRequest
from ..permissions import GROUPS_CREATE, GROUPS_DELETE, GROUPS_READ, GROUPS_UPDATE
from ..services.auth_service import AuthContext, AuthService
from ..services.users_service import UsersService
from ._routing import collection, new_router


def create_groups_router(
    auth_service: AuthService,
    users_service: UsersService,
    get_auth: Callable,
    require_permission: Callable[[str], Callable],
) -> APIRouter:
    router = new_router("/groups", "groups")
    group_store = auth_service.group_store
    user_store = auth_service.user_store

    async def load_group(name: str) -> Group:
        group = await group_store.get_group(name)
        if not group:
            raise not_found(f"group not found: {name}")
        return group

    async def load_user(id_or_name: str):
        user = await user_store.get_user(id_or_name)
        if not user:
            raise not_found(f"user not found: {id_or_name}")
        return user

    @collection(router, "GET")
    async def list_groups(ctx: AuthContext = Depends(get_auth)):
        return [g.to_dict() for g in await group_store.get_groups()]

    @collection(router, "POST", status_code=201)
    async def create_group(
        body: CreateGroupRequest,
        ctx: AuthContext = Depends(require_permission(GROUPS_CREATE)),
    ):
        group = await users_service.create_group(
            ctx, name=body.name, description=body.description, permissions=body.permissions,
        )
        return group.to_dict()

    @router.get("/{name}")
    async def get_group(name: str, ctx: AuthContext = Depends(get_auth)):
        return (await load_group(name)).to_dict()

    @router.get("/{name}/members")
    async def list_members(name: str, ctx: AuthContext = Depends(get_auth)):
        group = await load_group(name)
        if not ctx.can(GROUPS_READ) and not auth_service.in_group(ctx.user, group.name):
            raise forbidden(f"missing permission: {GROUPS_READ}")
        return [u.to_dict() for u in await users_service.group_members(group)]

    @router.put("/{name}")
    async def update_group(
        name: str,
        body: UpdateGroupRequest,
        ctx: AuthContext = Depends(require_permission(GROUPS_UPDATE)),
    ):
        group = await load_group(name)
        updated = await users_service.update_group(
            ctx, group, description=body.description, permissions=body.permissions,
        )
        return updated.to_dict()

    @router.delete("/{name}", status_code=204)
    async def delete_group(
        name: str,
        ctx: AuthContext = Depends(require_permission(GROUPS_DELETE)),
    ):
        await users_service.delete_group(ctx, await load_group(name))
        return Response(status_code=204)

    @router.post("/{name}/members/{id_or_name}", status_code=204)
    async def add_member(
        name: str,
        id_or_name: str,
        ctx: AuthContext = Depends(require_permission(GROUPS_UPDATE)),
    ):
        group = await group_store.get_group(name)
        if not group:
            # Contract: referenced groups must exist when adding a member → 400.
            raise bad_request(f"unknown group: {name}")
        user = await load_user(id_or_name)
        await users_service.add_member(ctx, group, user)
        return Response(status_code=204)

    @router.delete("/{name}/members/{id_or_name}", status_code=204)
    async def remove_member(
        name: str,
        id_or_name: str,
        ctx: AuthContext = Depends(require_permission(GROUPS_UPDATE)),
    ):
        group = await load_group(name)
        user = await load_user(id_or_name)
        await users_service.remove_member(ctx, group, user)
        return Response(status_code=204)

    return router
