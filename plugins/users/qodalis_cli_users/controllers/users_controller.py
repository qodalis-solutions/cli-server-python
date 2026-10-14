"""Users controller — /users routes (CRUD + password change)."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..models import (
    VerifyPasswordRequest,
    CreateUserRequest,
    PasswordChangeRequest,
    Session,
    UpdateUserRequest,
)
from ..services.auth_service import AuthService
from ..stores.interfaces import ICredentialStore, ISessionStore, IUserStore


def create_users_router(
    user_store: IUserStore,
    auth_service: AuthService,
    credential_store: ICredentialStore,
    session_store: ISessionStore,
    get_current_session: Callable,
    require_admin: Callable,
    *,
    external_auth: bool = False,
) -> APIRouter:
    router = APIRouter(prefix="/users", tags=["users"])

    # GET / — list all users
    @router.get("/")
    async def list_users(session: Session = Depends(get_current_session)):
        users = await user_store.get_users()
        return [u.to_dict() for u in users]

    # GET /{id_or_name} — get user by id or name
    @router.get("/{id_or_name}")
    async def get_user(id_or_name: str, session: Session = Depends(get_current_session)):
        user = await user_store.get_user(id_or_name)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user.to_dict()

    # POST / — create user (admin only)
    @router.post("/", status_code=201)
    async def create_user(body: CreateUserRequest, session: Session = Depends(require_admin)):
        try:
            user = await user_store.create_user(
                name=body.name,
                email=body.email,
                groups=body.groups,
                data=body.data,
            )
            if body.password:
                await auth_service.set_password(user.id, body.password)
            return user.to_dict()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    # PUT /{user_id} — update user (admin only)
    @router.put("/{user_id}")
    async def update_user(user_id: str, body: UpdateUserRequest, session: Session = Depends(require_admin)):
        try:
            user = await user_store.update_user(
                user_id,
                name=body.name,
                email=body.email,
                groups=body.groups,
                disabled=body.disabled,
                data=body.data,
            )
            return user.to_dict()
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    # DELETE /{user_id} — delete user (admin only)
    @router.delete("/{user_id}", status_code=204)
    async def delete_user(user_id: str, session: Session = Depends(require_admin)):
        await user_store.delete_user(user_id)
        await credential_store.delete_credentials(user_id)
        await session_store.delete_user_sessions(user_id)

    # PUT /{user_id}/password — change password (self or admin)
    @router.put("/{user_id}/password")
    async def change_password(
        user_id: str,
        body: PasswordChangeRequest,
        session: Session = Depends(get_current_session),
    ):
        requesting_user = session.user
        is_self = requesting_user.id == user_id
        is_admin = "admin" in (requesting_user.groups or [])

        if not is_self and not is_admin:
            raise HTTPException(status_code=403, detail="Forbidden")

        if is_self and not is_admin:
            if not body.currentPassword:
                raise HTTPException(status_code=400, detail="currentPassword is required")
            valid = await auth_service.verify_password(user_id, body.currentPassword)
            if not valid:
                # 403, not 401: the session is valid, the supplied password is not
                raise HTTPException(status_code=403, detail="Current password is incorrect")

        await auth_service.set_password(user_id, body.newPassword)
        return {"message": "Password updated"}

    # POST /{user_id}/verify-password — check a user's password, returns {"valid": bool}.
    # Requires an authenticated session (used by `su` and `passwd` on the client).
    @router.post("/{user_id}/verify-password")
    async def verify_password(
        user_id: str,
        body: VerifyPasswordRequest,
        session: Session = Depends(get_current_session),
    ):
        if external_auth:
            return JSONResponse(
                status_code=501,
                content={"error": "Authentication is managed by the host application"},
            )
        valid = await auth_service.verify_password(user_id, body.password)
        return {"valid": valid}

    return router
