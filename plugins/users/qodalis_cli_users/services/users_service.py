"""User and group management — validation, privilege checks, protections,
cascades and audit.

Controllers check endpoint permissions; this service enforces the data and
security rules shared by every caller.
"""

from __future__ import annotations

from typing import Any

from ..errors import bad_request, conflict, not_found
from ..models import Group, User
from ..permissions import ADMIN_GROUP, USERS_GROUP
from ..validation import (
    DESCRIPTION_MAX,
    NAME_MAX,
    validate_data,
    validate_email,
    validate_name,
    validate_permissions,
    validate_text,
)
from .auth_service import AuthContext, AuthService

BUILT_IN_GROUPS = (ADMIN_GROUP, USERS_GROUP)

LAST_ADMIN = "cannot remove the last admin"
HOME_DIR_MAX = 1024


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _actor(ctx: AuthContext | None) -> dict[str, Any]:
    if ctx is None:
        return {}
    return {"actor": ctx.user.name, "actor_id": ctx.user.id}


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
            group = await self.groups.get_group(name) if len(name) <= NAME_MAX else None
            if not group:
                raise bad_request(f"unknown group: {name[:NAME_MAX]}")
            if not any(g.lower() == group.name.lower() for g in out):
                out.append(group.name)
        return out

    async def _ensure_unique(self, name: str | None, email: str | None, exclude_id: str | None) -> None:
        """Names and emails share one namespace (case-insensitive)."""
        for user in await self.users.get_users():
            if user.id == exclude_id:
                continue
            taken = {user.name.lower(), user.email.lower()}
            if name is not None and name.lower() in taken:
                raise conflict(f"user already exists: {name}")
            if email is not None and email.lower() in taken:
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
        name = validate_name(_clean(name))
        email = validate_email(_clean(email))
        validate_text(home_dir, "homeDir", HOME_DIR_MAX)
        validate_data(data)
        if password is not None:
            error = self.auth.validate_password_policy(password)
            if error:
                raise bad_request(error)
        resolved = await self.resolve_groups(groups)
        if resolved and ctx is not None:
            await self.auth.ensure_can_grant_groups(ctx, resolved)
        if not resolved:
            resolved = await self.resolve_groups([USERS_GROUP])
        await self._ensure_unique(name, email, None)
        try:
            user = await self.users.create_user(
                name, email, resolved, data,
                disabled=bool(disabled), home_dir=home_dir,
            )
        except ValueError as exc:
            raise conflict(str(exc)) from exc
        if password is not None:
            await self.auth.set_password(user.id, password)
        await self.auth.audit("user_created", user.name, user_id=user.id, **_actor(ctx))
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
            name = validate_name(_clean(name))
            if name == target.name:
                name = None
        if email is not None:
            email = validate_email(_clean(email))
            if email == target.email:
                email = None
        validate_text(home_dir, "homeDir", HOME_DIR_MAX)
        validate_data(data)

        resolved_groups: list[str] | None = None
        if groups is not None:
            resolved_groups = await self.resolve_groups(groups)

        if ctx is not None:
            await self.auth.ensure_covers_user(ctx, target)
            if resolved_groups is not None:
                added = [g for g in resolved_groups if not self.auth.in_group(target, g)]
                if added:
                    await self.auth.ensure_can_grant_groups(ctx, added)

        await self._ensure_unique(name, email, target.id)

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
        await self.auth.audit("user_updated", user.name, user_id=user.id, **actor)
        if disabled is not None and disabled != was_disabled:
            if disabled:
                await self.auth.audit("user_disabled", user.name, user_id=user.id, **actor)
                await self.auth.revoke_user_sessions(user.id, "user_disabled", **actor)
            else:
                await self.auth.audit("user_enabled", user.name, user_id=user.id, **actor)
        return user

    async def delete_user(self, ctx: AuthContext | None, target: User) -> None:
        if ctx is not None and ctx.is_self(target):
            raise conflict("cannot delete yourself")
        if ctx is not None:
            await self.auth.ensure_covers_user(ctx, target)
        if await self.auth.is_last_enabled_admin(target):
            raise conflict(LAST_ADMIN)
        await self.users.delete_user(target.id)
        await self.credentials.delete_credentials(target.id)
        self.auth.reset_failures(target.id)
        actor = _actor(ctx)
        # Sessions first (sends session-kicked), then the user's API tokens.
        await self.auth.revoke_user_sessions(target.id, "user_deleted", **actor)
        await self.auth.api_token_store.delete_user_tokens(target.id)
        self.auth.notify_change()
        await self.auth.audit("user_deleted", target.name, user_id=target.id, **actor)

    async def change_password(
        self, ctx: AuthContext, target: User, password: str, *, temporary: bool | None = None,
    ) -> None:
        """Set ``target``'s password.  By self: clears mustChangePassword and
        revokes the caller's other sessions.  Admin reset: clears the
        target's lockout, revokes all their sessions and sets
        mustChangePassword unless ``temporary`` is ``False``."""
        error = self.auth.validate_password_policy(password)
        if error:
            raise bad_request(error)
        is_self = ctx.is_self(target)
        if not is_self:
            await self.auth.ensure_covers_user(ctx, target)
        await self.auth.set_password(target.id, password)
        actor = _actor(ctx)
        if is_self:
            if target.must_change_password:
                await self.users.update_user(target.id, must_change_password=False)
                ctx.user.must_change_password = False
            await self.auth.audit("password_changed", target.name, user_id=target.id, **actor)
            await self.auth.revoke_user_sessions(
                target.id, "password_changed",
                except_session_id=ctx.session.id if ctx.session else None, **actor,
            )
        else:
            self.auth.reset_failures(target.id)
            await self.users.update_user(target.id, must_change_password=temporary is not False)
            await self.auth.audit("password_changed", target.name, user_id=target.id, **actor)
            await self.auth.revoke_user_sessions(target.id, "password_changed", **actor)

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    async def group_members(self, group: Group) -> list[User]:
        return [u for u in await self.users.get_users() if self.auth.in_group(u, group.name)]

    async def create_group(
        self,
        ctx: AuthContext | None,
        *,
        name: str,
        description: str | None = None,
        permissions: list[str] | None = None,
    ) -> Group:
        name = validate_name(_clean(name))
        validate_text(description, "description", DESCRIPTION_MAX)
        perms = validate_permissions(permissions) or []
        if ctx is not None:
            self.auth.ensure_can_grant_permissions(ctx, perms)
        if await self.groups.get_group(name):
            raise conflict(f"group already exists: {name}")
        try:
            group = await self.groups.create_group(name, description, perms)
        except ValueError as exc:
            raise conflict(str(exc)) from exc
        await self.auth.audit("group_created", ctx.user.name if ctx else "", details=group.name, **_actor(ctx))
        return group

    async def update_group(
        self,
        ctx: AuthContext | None,
        group: Group,
        *,
        description: str | None = None,
        permissions: list[str] | None = None,
    ) -> Group:
        validate_text(description, "description", DESCRIPTION_MAX)
        perms = validate_permissions(permissions)
        if perms is not None and group.name.lower() == ADMIN_GROUP:
            raise conflict("cannot change the permissions of the admin group")
        if perms is not None and ctx is not None:
            self.auth.ensure_can_grant_permissions(ctx, perms)
        try:
            updated = await self.groups.update_group(
                group.name, description=description, permissions=perms,
            )
        except KeyError as exc:
            raise not_found(f"group not found: {group.name}") from exc
        await self.auth.audit("group_updated", ctx.user.name if ctx else "", details=updated.name, **_actor(ctx))
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
        await self.auth.audit("group_deleted", ctx.user.name if ctx else "", details=group.name, **_actor(ctx))

    async def add_member(self, ctx: AuthContext | None, group: Group, user: User) -> None:
        if ctx is not None:
            await self.auth.ensure_covers_user(ctx, user)
            await self.auth.ensure_can_grant_groups(ctx, [group.name])
        if self.auth.in_group(user, group.name):
            return
        await self.users.update_user(user.id, groups=[*user.groups, group.name])
        await self.auth.audit("group_member_added", user.name, details=group.name,
                              user_id=user.id, **_actor(ctx))

    async def remove_member(self, ctx: AuthContext | None, group: Group, user: User) -> None:
        if ctx is not None:
            await self.auth.ensure_covers_user(ctx, user)
        if not self.auth.in_group(user, group.name):
            return
        if group.name.lower() == ADMIN_GROUP and await self.auth.is_last_enabled_admin(user):
            raise conflict(LAST_ADMIN)
        await self.users.update_user(
            user.id,
            groups=[g for g in user.groups if g.lower() != group.name.lower()],
        )
        await self.auth.audit("group_member_removed", user.name, details=group.name,
                              user_id=user.id, **_actor(ctx))
