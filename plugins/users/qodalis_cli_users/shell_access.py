"""Remote shell access for the users plugin (Appendix C, ``users`` mode).

Registered on the CLI server when the plugin is mounted.  An open shell is
bound to the session that opened it: the server re-checks it periodically and
on every session/user/group change, and closes it (4401 ``session ended``)
after logout, expiry, revoke, disable/delete or loss of ``shell:execute``.
Identities from an external auth adapter are re-validated by calling the
adapter again with the shell's WebSocket request, so the adapter must be able
to authenticate it from its headers/cookies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs

from ._http import get_scope_auth
from .permissions import has_permission
from .services.auth_service import AuthContext, AuthService

SHELL_PERMISSION = "shell:execute"
#: ``shell_closed`` audit reason when ``shell:execute`` is lost.
PERMISSION_REMOVED = "permission removed"


@dataclass
class UsersShellPrincipal:
    """The caller owning a shell (duck-type of ``qodalis_cli.ShellPrincipal``)."""

    user_id: str
    user_name: str
    session_id: str | None
    context: AuthContext
    #: The shell's WebSocket: adapter identities are re-validated by calling
    #: the external auth adapter again with it.
    connection: Any = None


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
            connection=websocket,
        )

    def can_open(self, principal: UsersShellPrincipal) -> bool:
        ctx = principal.context
        return not ctx.must_change_password and ctx.can(SHELL_PERMISSION)

    async def is_active(self, principal: UsersShellPrincipal) -> bool | str:
        """``True`` while valid; ``False`` (session ended) or
        ``"permission removed"`` otherwise.  Never extends the session."""
        ctx = principal.context
        if ctx.external:
            return await self._adapter_active(principal)
        if principal.session_id is None:
            return False
        session = await self.auth.session_store.get_session_by_id(principal.session_id)
        if session is None or session.user_id != principal.user_id or self.auth.is_expired(session):
            return False
        user = await self.auth.user_store.get_user(principal.user_id)
        if user is None or user.id != principal.user_id or user.disabled or user.must_change_password:
            return False
        if not has_permission(await self.auth.user_permissions(user), SHELL_PERMISSION):
            return PERMISSION_REMOVED
        return True

    async def _adapter_active(self, principal: UsersShellPrincipal) -> bool | str:
        """Adapter identities have no stored session: ask the external auth
        adapter again with the shell's WebSocket (its headers and cookies).
        ``None`` or an exception ends the shell; a different user or one
        without ``shell:execute`` too."""
        if principal.connection is None or not self.auth.external_auth:
            return False
        try:
            fresh = await self.auth.authenticate_external(principal.connection)
        except Exception:
            return False
        if fresh.user.id != principal.user_id or fresh.must_change_password:
            return False
        if not fresh.can(SHELL_PERMISSION):
            return PERMISSION_REMOVED
        return True

    async def touch(self, principal: UsersShellPrincipal) -> None:
        """Count shell input as session activity (idle timeout)."""
        if principal.context.external or principal.session_id is None:
            return
        session = await self.auth.session_store.get_session_by_id(principal.session_id)
        if session is not None and session.token_hash:
            await self.auth.session_store.update_last_activity(session.token_hash)

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
