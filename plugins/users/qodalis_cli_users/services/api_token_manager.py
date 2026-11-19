"""Host-facing API token manager (``users_plugin.api_tokens``).

The host application creates, lists and revokes its users' API tokens with
the validation, limits, audit entries and events of ``/tokens``, and creates
``host`` tokens: tokens only its own guarded routes (``require_auth`` /
``require_permission``) accept, scoped to ``host`` permissions of the
catalog.  Errors are :class:`UsersApiError` with the status and text the
HTTP route would answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import UsersApiError, bad_request, forbidden, not_found
from ..models import User, now_ms
from ..permissions import has_permission
from .auth_service import ACCOUNT_DISABLED

if TYPE_CHECKING:
    from .auth_service import AuthService

USER_NOT_FOUND = "user not found"


@dataclass
class ApiTokenCreateOptions:
    """A token to create (``users_plugin.api_tokens.create``)."""

    name: str
    #: 1-365; ``None`` = the default 90 days.
    expires_in_days: int | None = None
    #: Never expires (needs ``set_allow_non_expiring_tokens()``).
    never_expires: bool = False
    #: The scope; ``None`` = the user's full live permissions (``cli`` only).
    permissions: list[str] | None = None
    #: ``cli`` or ``host``.
    audience: str = "cli"
    #: The audit actor name (default the token owner's name).
    actor: str | None = None


@dataclass
class CreatedApiToken:
    """The raw token (shown once) and its view, as ``POST /tokens`` answers."""

    token: str
    api_token: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"token": self.token, "apiToken": self.api_token}


class ApiTokenManager:
    """API tokens a host creates, lists and revokes for its users."""

    def __init__(self, auth_service: AuthService) -> None:
        self.auth_service = auth_service

    async def _user(self, user_id: str) -> User:
        user = await self.auth_service.user_store.get_user(user_id)
        if user is None or user.id != user_id:
            raise not_found(USER_NOT_FOUND)
        return user

    @staticmethod
    def _audience(audience: Any) -> str:
        from ..controllers.tokens_controller import API_TOKEN_AUDIENCES, INVALID_AUDIENCE

        if not isinstance(audience, str) or audience not in API_TOKEN_AUDIENCES:
            raise bad_request(INVALID_AUDIENCE)
        return audience

    async def create(self, user_id: str, options: ApiTokenCreateOptions) -> CreatedApiToken:
        """Create a token for ``user_id``; the rules apply in this order:
        API tokens enabled (501), user (404 / 403 disabled), audience,
        name, expiry, permission syntax, host permissions, permissions held
        (403), name taken (409) and token limit (400)."""
        # Imported here: the tokens controller imports the services package.
        from ..controllers.tokens_controller import (
            EXPIRY_CONFLICT,
            MANAGED_BY_HOST,
            create_api_token,
            default_token_expiry,
            ensure_grantable,
            host_scope,
            parse_scope,
            token_expiry,
            validate_token_name,
        )

        auth = self.auth_service
        if auth.external_auth and not auth.options.api_tokens_with_external_auth:
            raise UsersApiError(501, MANAGED_BY_HOST)
        user = await self._user(user_id)
        if user.disabled:
            raise forbidden(ACCOUNT_DISABLED)
        audience = self._audience(options.audience)
        name = validate_token_name(options.name)
        now = now_ms()
        if options.never_expires:
            if options.expires_in_days is not None:
                raise bad_request(EXPIRY_CONFLICT)
            expires_at = token_expiry(None, auth.options.allow_non_expiring_tokens, now)
        elif options.expires_in_days is None:
            expires_at = default_token_expiry(now)
        else:
            expires_at = token_expiry(options.expires_in_days, False, now)
        permissions = parse_scope(options.permissions)
        if audience == "host":
            permissions = host_scope(auth.permission_catalog, permissions)
        ensure_grantable(await auth.user_permissions(user), permissions)
        actor = (options.actor, None) if options.actor else (user.name, user.id)
        token, record = await create_api_token(
            auth, user, actor, name, permissions, expires_at, audience,
        )
        return CreatedApiToken(token=token, api_token=record.to_view(user.name))

    async def list(self, user_id: str) -> list[dict[str, Any]]:
        """The user's tokens (both audiences), oldest first; ``[]`` for an
        unknown user."""
        tokens = await self.auth_service.api_token_store.get_tokens(user_id)
        tokens.sort(key=lambda t: t.created_at)
        return [await self.auth_service.api_token_view(t) for t in tokens]

    async def revoke(self, user_id: str, token_id: str, actor: str | None = None) -> bool:
        """Revoke the user's token; ``False`` when it does not exist or
        belongs to another user."""
        from ..controllers.tokens_controller import revoke_api_token

        auth = self.auth_service
        record = await auth.api_token_store.get_token(token_id)
        if record is None or record.user_id != user_id:
            return False
        if actor:
            by: tuple[str, str | None] = (actor, None)
        else:
            owner = await auth.user_store.get_user(user_id)
            by = (owner.name, owner.id) if owner is not None else (user_id, user_id)
        await revoke_api_token(auth, by, record)
        return True

    async def grantable_permissions(self, user_id: str, audience: str = "cli") -> list[Any]:
        """The catalog entries the user holds (live permissions, wildcards
        match); for ``host`` only the ``host``-surface ones."""
        user = await self._user(user_id)
        audience = self._audience(audience)
        granted = await self.auth_service.user_permissions(user)
        return [
            p for p in self.auth_service.permission_catalog.all()
            if (audience == "cli" or p.surface == "host") and has_permission(granted, p.name)
        ]
