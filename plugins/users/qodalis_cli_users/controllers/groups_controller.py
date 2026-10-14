"""Groups controller — /groups routes."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException

from ..models import CreateGroupRequest, Session
from ..stores.interfaces import IGroupStore, IUserStore


def create_groups_router(
    group_store: IGroupStore,
    user_store: IUserStore,
    get_current_session: Callable,
    require_admin: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/groups", tags=["groups"])

    # GET / — list all groups
    @router.get("/")
    async def list_groups(session: Session = Depends(get_current_session)):
        groups = await group_store.get_groups()
        return [g.to_dict() for g in groups]

    # GET /{name} — get group by name
    @router.get("/{name}")
    async def get_group(name: str, session: Session = Depends(get_current_session)):
        group = await group_store.get_group(name)
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")
        return group.to_dict()

    # POST / — create group (admin only)
    @router.post("/", status_code=201)
    async def create_group(body: CreateGroupRequest, session: Session = Depends(require_admin)):
        try:
            group = await group_store.create_group(body.name, body.description)
            return group.to_dict()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    # DELETE /{name} — delete group (admin only)
    @router.delete("/{name}", status_code=204)
    async def delete_group(name: str, session: Session = Depends(require_admin)):
        await group_store.delete_group(name)

    # PUT /{name}/members/{user_id} — add user to group (admin only)
    @router.put("/{name}/members/{user_id}")
    async def add_member(name: str, user_id: str, session: Session = Depends(require_admin)):
        group = await group_store.get_group(name)
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")

        user = await user_store.get_user(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if name not in user.groups:
            await user_store.update_user(user_id, groups=[*user.groups, name])

        return {"message": f"User added to group '{name}'"}

    # DELETE /{name}/members/{user_id} — remove user from group (admin only)
    @router.delete("/{name}/members/{user_id}")
    async def remove_member(name: str, user_id: str, session: Session = Depends(require_admin)):
        user = await user_store.get_user(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        await user_store.update_user(user_id, groups=[g for g in user.groups if g != name])
        return {"message": f"User removed from group '{name}'"}

    return router
