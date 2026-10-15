"""Remote shell access for the users plugin (Appendix C, ``users`` mode).

Registered on the CLI server when the plugin is mounted.  An open shell is
bound to the session that opened it: the server re-checks it periodically and
on every session/user/group change, and closes it (4401 ``session ended``)
after logout, expiry, revoke, disable/delete or loss of ``shell:execute``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs

from ._http import get_scope_auth
from .permissions import has_permission
from .services.auth_service import AuthContext, AuthService

SHELL_PERMISSION = "shell:execute"


@dataclass
class UsersShellPrincipal:
    """The caller owning a shell (duck-type of ``qodalis_cli.ShellPrincipal``)."""

    user_id: str
    user_name: str
    session_id: str | None
    context: AuthContext


class UsersShellAccess:
    """``ShellAccessProvider`` backed by the users plugin's sessions."""

    def __init__(self, auth_service: AuthService) -> None:
        self.auth = auth_service

    async def resolve(self, websocket: Any) -> UsersShellPrincipal | None:
        # Normally set by the server-protection middleware (ticket consumed);
        # without it (protect_server=False) the shell still requires a ticket.
        ctx = get_scope_auth(websocket.scope)
        if not isinstance(ctx, AuthContext):
            raw = websocket.scope.get("query_string", b"").decode("latin-1")
            ticket = (parse_qs(raw).get("ticket") or [None])[0]
            ctx = await self.auth.consume_ticket(ticket)
        if ctx is None:
            return None
        return UsersShellPrincipal(
            user_id=ctx.user.id,
            user_name=ctx.user.name,
            session_id=ctx.session.id if ctx.session else None,
            context=ctx,
        )

    def can_open(self, principal: UsersShellPrincipal) -> bool:
        ctx = principal.context
        return not ctx.must_change_password and ctx.can(SHELL_PERMISSION)

    async def is_active(self, principal: UsersShellPrincipal) -> bool:
        ctx = principal.context
        if ctx.external:
            # Adapter identities have no stored session to re-check.
            return True
        if principal.session_id is None:
            return False
        session = await self.auth.session_store.get_session_by_id(principal.session_id)
        if session is None or session.user_id != principal.user_id or self.auth.is_expired(session):
            return False
        user = await self.auth.user_store.get_user(principal.user_id)
        if user is None or user.id != principal.user_id or user.disabled or user.must_change_password:
            return False
        return has_permission(await self.auth.user_permissions(user), SHELL_PERMISSION)

    async def audit(self, event: str, principal: UsersShellPrincipal, metadata: dict[str, Any]) -> None:
        await self.auth.audit(
            event,
            principal.user_name,
            user_id=principal.user_id,
            actor=principal.user_name,
            actor_id=principal.user_id,
            metadata=metadata,
        )

    def add_change_listener(self, listener: Callable[[], Any]) -> None:
        self.auth.add_change_listener(listener)


__all__ = ["SHELL_PERMISSION", "UsersShellAccess", "UsersShellPrincipal"]
