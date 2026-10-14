"""User and group management — validation, protections, cascades, audit.

Controllers check endpoint permissions; this service enforces the data rules
shared by every caller (duplicate names, unknown groups, last-admin and
built-in protections, cascades).
"""

from __future__ import annotations

from typing import Any

from ..errors import bad_request, conflict, not_found
from ..models import Group, User
from ..permissions import ADMIN_GROUP, USERS_GROUP
from .auth_service import AuthContext, AuthService

BUILT_IN_GROUPS = (ADMIN_GROUP, USERS_GROUP)

LAST_ADMIN = "cannot remove the last admin"


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _actor(ctx: AuthContext | None) -> str | None:
    return ctx.user.name if ctx else None


class UsersService:
    def __init__(self, auth: AuthService) -> None:
        self.auth = auth
        self.users = auth.user_store
        self.groups = auth.group_store
        self.credentials = auth.credential_store

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def resolve_groups(self, names: list[str] | None) -> list[str]:
        """Validate group names; return the stored (canonical) names,
        de-duplicated case-insensitively."""
        out: list[str] = []
        for raw in names or []:
            name = _clean(raw)
            if not name:
                continue
            group = await self.groups.get_group(name)
            if not group:
                raise bad_request(f"unknown group: {name}")
            if not any(g.lower() == group.name.lower() for g in out):
                out.append(group.name)
        return out

    async def _ensure_unique(self, name: str | None, email: str | None, exclude_id: str | None) -> None:
        for user in await self.users.get_users():
            if user.id == exclude_id:
                continue
            if name is not None and user.name.lower() == name.lower():
                raise conflict(f"user already exists: {name}")
            if email is not None and user.email.lower() == email.lower():
                raise conflict(f"email already in use: {email}")

    @staticmethod
    def _is_built_in(group: Group) -> bool:
        return group.built_in or group.name.lower() in BUILT_IN_GROUPS

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def list_users(self, query: str | None, skip: int, take: int | None) -> list[User]:
        users = await self.users.get_users()
        q = _clean(query).lower()
        if q:
            users = [u for u in users if q in u.name.lower() or q in u.email.lower()]
        users = users[skip:]
        if take is not None:
            users = users[:take]
        return users

    async def create_user(
        self,
        ctx: AuthContext | None,
        *,
        name: str,
        email: str,
        groups: list[str] | None = None,
        password: str | None = None,
        disabled: bool | None = None,
        home_dir: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> User:
        name = _clean(name)
        email = _clean(email)
        if not name:
            raise bad_request("name is required")
        if not email:
            raise bad_request("email is required")
        await self._ensure_unique(name, email, None)
        resolved = await self.resolve_groups(groups)
        if not resolved:
            resolved = await self.resolve_groups([USERS_GROUP])
        if password is not None:
            error = self.auth.validate_password_policy(password)
            if error:
                raise bad_request(error)
        try:
            user = await self.users.create_user(
                name, email, resolved, data,
                disabled=bool(disabled), home_dir=home_dir,
            )
        except ValueError as exc:
            raise conflict(str(exc)) from exc
        if password is not None:
            await self.auth.set_password(user.id, password)
        await self.auth.audit("user_created", user.name, actor=_actor(ctx))
        return user

    async def update_user(
        self,
        ctx: AuthContext | None,
        target: User,
        *,
        name: str | None = None,
        email: str | None = None,
        groups: list[str] | None = None,
        disabled: bool | None = None,
        home_dir: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> User:
        if name is not None:
            name = _clean(name)
            if not name:
                raise bad_request("name must not be empty")
            if name == target.name:
                name = None
        if email is not None:
            email = _clean(email)
            if not email:
                raise bad_request("email must not be empty")
            if email == target.email:
                email = None
        await self._ensure_unique(name, email, target.id)

        resolved_groups: list[str] | None = None
        if groups is not None:
            resolved_groups = await self.resolve_groups(groups)

        removes_admin = resolved_groups is not None and not any(
            g.lower() == ADMIN_GROUP for g in resolved_groups
        )
        if (removes_admin or disabled is True) and await self.auth.is_last_enabled_admin(target):
            raise conflict(LAST_ADMIN)

        was_disabled = bool(target.disabled)
        try:
            user = await self.users.update_user(
                target.id,
                name=name,
                email=email,
                groups=resolved_groups,
                disabled=disabled,
                home_dir=home_dir,
                data=data,
            )
        except KeyError as exc:
            raise not_found(f"user not found: {target.name}") from exc

        actor = _actor(ctx)
        await self.auth.audit("user_updated", user.name, actor=actor)
        if disabled is not None and disabled != was_disabled:
            if disabled:
                await self.auth.audit("user_disabled", user.name, actor=actor)
                await self.auth.revoke_user_sessions(user.id, "user_disabled", actor=actor)
            else:
                await self.auth.audit("user_enabled", user.name, actor=actor)
        return user

    async def delete_user(self, ctx: AuthContext | None, target: User) -> None:
        if ctx is not None and ctx.is_self(target):
            raise conflict("cannot delete yourself")
        if await self.auth.is_last_enabled_admin(target):
            raise conflict(LAST_ADMIN)
        await self.users.delete_user(target.id)
        await self.credentials.delete_credentials(target.id)
        self.auth.reset_failures(target.id)
        actor = _actor(ctx)
        await self.auth.revoke_user_sessions(target.id, "user_deleted", actor=actor)
        await self.auth.audit("user_deleted", target.name, actor=actor)

    async def change_password(
        self, ctx: AuthContext, target: User, password: str,
    ) -> None:
        error = self.auth.validate_password_policy(password)
        if error:
            raise bad_request(error)
        await self.auth.set_password(target.id, password)
        await self.auth.audit("password_changed", target.name, actor=ctx.user.name)
        if ctx.is_self(target):
            await self.auth.revoke_user_sessions(
                target.id, "password_changed", actor=ctx.user.name,
                except_session_id=ctx.session.id if ctx.session else None,
            )
        else:
            await self.auth.revoke_user_sessions(
                target.id, "password_changed", actor=ctx.user.name,
            )

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    async def group_members(self, group: Group) -> list[User]:
        return [u for u in await self.users.get_users() if self.auth.in_group(u, group.name)]

    @staticmethod
    def _clean_permissions(permissions: list[str] | None) -> list[str]:
        out: list[str] = []
        for p in permissions or []:
            p = _clean(p)
            if p and p.lower() not in (x.lower() for x in out):
                out.append(p)
        return out

    async def create_group(
        self,
        ctx: AuthContext | None,
        *,
        name: str,
        description: str | None = None,
        permissions: list[str] | None = None,
    ) -> Group:
        name = _clean(name)
        if not name:
            raise bad_request("name is required")
        if await self.groups.get_group(name):
            raise conflict(f"group already exists: {name}")
        try:
            group = await self.groups.create_group(
                name, description, self._clean_permissions(permissions),
            )
        except ValueError as exc:
            raise conflict(str(exc)) from exc
        await self.auth.audit("group_created", _actor(ctx) or "", details=group.name, actor=_actor(ctx))
        return group

    async def update_group(
        self,
        ctx: AuthContext | None,
        group: Group,
        *,
        description: str | None = None,
        permissions: list[str] | None = None,
    ) -> Group:
        if permissions is not None and group.name.lower() == ADMIN_GROUP:
            raise conflict("cannot change the permissions of the admin group")
        try:
            updated = await self.groups.update_group(
                group.name,
                description=description,
                permissions=self._clean_permissions(permissions) if permissions is not None else None,
            )
        except KeyError as exc:
            raise not_found(f"group not found: {group.name}") from exc
        await self.auth.audit("group_updated", _actor(ctx) or "", details=updated.name, actor=_actor(ctx))
        return updated

    async def delete_group(self, ctx: AuthContext | None, group: Group) -> None:
        if self._is_built_in(group):
            raise conflict(f"cannot delete built-in group: {group.name}")
        for user in await self.group_members(group):
            await self.users.update_user(
                user.id,
                groups=[g for g in user.groups if g.lower() != group.name.lower()],
            )
        await self.groups.delete_group(group.name)
        await self.auth.audit("group_deleted", _actor(ctx) or "", details=group.name, actor=_actor(ctx))

    async def add_member(self, ctx: AuthContext | None, group: Group, user: User) -> None:
        if self.auth.in_group(user, group.name):
            return
        await self.users.update_user(user.id, groups=[*user.groups, group.name])
        await self.auth.audit("group_member_added", user.name, details=group.name, actor=_actor(ctx))

    async def remove_member(self, ctx: AuthContext | None, group: Group, user: User) -> None:
        if not self.auth.in_group(user, group.name):
            return
        if group.name.lower() == ADMIN_GROUP and await self.auth.is_last_enabled_admin(user):
            raise conflict(LAST_ADMIN)
        await self.users.update_user(
            user.id,
            groups=[g for g in user.groups if g.lower() != group.name.lower()],
        )
        await self.auth.audit("group_member_removed", user.name, details=group.name, actor=_actor(ctx))
