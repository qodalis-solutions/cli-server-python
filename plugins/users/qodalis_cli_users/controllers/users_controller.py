"""Users controller — ``/users`` (CRUD, permissions, password)."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, Query, Response

from ..errors import UsersApiError, bad_request, error_response, forbidden, not_found
from ..models import (
    CreateUserRequest,
    PasswordChangeRequest,
    UpdateUserRequest,
    User,
    VerifyPasswordRequest,
)
from ..permissions import USERS_CREATE, USERS_DELETE, USERS_READ, USERS_UPDATE
from ..services.auth_service import ACCOUNT_LOCKED, AuthContext, AuthService
from ..services.users_service import UsersService
from ._routing import collection, new_router

MANAGED_BY_HOST = "Authentication is managed by the host application"


def create_users_router(
    auth_service: AuthService,
    users_service: UsersService,
    get_auth: Callable,
    require_permission: Callable[[str], Callable],
) -> APIRouter:
    router = new_router("/users", "users")
    user_store = auth_service.user_store
    external = auth_service.external_auth

    async def load_target(id_or_name: str, ctx: AuthContext, permission: str, *, allow_self: bool) -> User:
        """Resolve the target user, enforcing ``permission`` (or self).

        Callers lacking the permission get 403 rather than 404 so user
        existence is not leaked to them.
        """
        target = await user_store.get_user(id_or_name)
        allowed = ctx.can(permission) or (allow_self and ctx.is_self(target))
        if not allowed:
            raise forbidden(f"missing permission: {permission}")
        if target is None:
            raise not_found(f"user not found: {id_or_name}")
        return target

    @collection(router, "GET")
    async def list_users(
        query: str | None = None,
        skip: int = Query(default=0, ge=0),
        take: int | None = Query(default=None, ge=0),
        ctx: AuthContext = Depends(require_permission(USERS_READ)),
    ):
        users = await users_service.list_users(query, skip, take)
        return [u.to_dict() for u in users]

    @collection(router, "POST", status_code=201)
    async def create_user(
        body: CreateUserRequest,
        ctx: AuthContext = Depends(require_permission(USERS_CREATE)),
    ):
        user = await users_service.create_user(
            ctx,
            name=body.name,
            email=body.email,
            groups=body.groups,
            password=body.password,
            disabled=body.disabled,
            home_dir=body.homeDir,
            data=body.data,
        )
        return user.to_dict()

    @router.get("/{id_or_name}")
    async def get_user(id_or_name: str, ctx: AuthContext = Depends(get_auth)):
        target = await load_target(id_or_name, ctx, USERS_READ, allow_self=True)
        return target.to_dict()

    @router.get("/{id_or_name}/permissions")
    async def get_user_permissions(id_or_name: str, ctx: AuthContext = Depends(get_auth)):
        target = await load_target(id_or_name, ctx, USERS_READ, allow_self=True)
        groups = list(target.groups or [])
        return {
            "userId": target.id,
            "groups": groups,
            "permissions": await auth_service.effective_permissions(groups),
        }

    @router.put("/{id_or_name}")
    async def update_user(
        id_or_name: str,
        body: UpdateUserRequest,
        ctx: AuthContext = Depends(get_auth),
    ):
        target = await load_target(id_or_name, ctx, USERS_UPDATE, allow_self=True)
        if not ctx.can(USERS_UPDATE):
            # Self-service: only email and data may change.
            restricted = [
                f for f, v in (
                    ("name", body.name),
                    ("groups", body.groups),
                    ("disabled", body.disabled),
                    ("homeDir", body.homeDir),
                ) if v is not None
            ]
            if restricted:
                raise forbidden(f"missing permission: {USERS_UPDATE}")
        user = await users_service.update_user(
            ctx,
            target,
            name=body.name,
            email=body.email,
            groups=body.groups,
            disabled=body.disabled,
            home_dir=body.homeDir,
            data=body.data,
        )
        return user.to_dict()

    @router.delete("/{id_or_name}", status_code=204)
    async def delete_user(
        id_or_name: str,
        ctx: AuthContext = Depends(require_permission(USERS_DELETE)),
    ):
        target = await load_target(id_or_name, ctx, USERS_DELETE, allow_self=False)
        await users_service.delete_user(ctx, target)
        return Response(status_code=204)

    if external:
        @router.put("/{id_or_name}/password")
        async def change_password_external(id_or_name: str):
            return error_response(501, MANAGED_BY_HOST)

        @router.post("/{id_or_name}/verify-password")
        async def verify_password_external(id_or_name: str):
            return error_response(501, MANAGED_BY_HOST)

        return router

    @router.put("/{id_or_name}/password", status_code=204)
    async def change_password(
        id_or_name: str,
        body: PasswordChangeRequest,
        ctx: AuthContext = Depends(get_auth),
    ):
        target = await load_target(id_or_name, ctx, USERS_UPDATE, allow_self=True)
        password = body.password if body.password is not None else body.newPassword
        if password is None:
            raise bad_request("password is required")
        if ctx.is_self(target) and not ctx.can(USERS_UPDATE):
            if not body.currentPassword:
                raise bad_request("currentPassword is required")
            if not await auth_service.verify_password(target.id, body.currentPassword):
                raise forbidden("current password is incorrect")
        await users_service.change_password(ctx, target, password)
        return Response(status_code=204)

    @router.post("/{id_or_name}/verify-password")
    async def verify_password(
        id_or_name: str,
        body: VerifyPasswordRequest,
        ctx: AuthContext = Depends(get_auth),
    ):
        target = await load_target(id_or_name, ctx, USERS_UPDATE, allow_self=True)
        password = body.password
        if auth_service.is_locked(target.id):
            raise UsersApiError(429, ACCOUNT_LOCKED)
        valid = await auth_service.verify_password(target.id, password)
        if valid:
            auth_service.reset_failures(target.id)
        else:
            auth_service.register_failure(target.id)
            await auth_service.audit(
                "login_failed", target.name,
                details="password verification failed", actor=ctx.user.name,
            )
        return {"valid": valid}

    return router
