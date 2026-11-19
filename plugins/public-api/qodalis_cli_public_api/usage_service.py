"""Token and account usage of the public API: the token views' ``usage``
(users plugin hook) and ``public_api.usage.account(user_id)`` for a host's own
pages."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from qodalis_cli_users import QcliCaller, UsersApiError

from .quota import PublicApiQuota, QuotaResolver
from .usage_store import IPublicApiUsageStore

USAGE_DAYS = 30
USER_NOT_FOUND = "user not found"


def utc_now(clock: Callable[[], datetime]) -> datetime:
    """The clock's time in UTC (a naive value is read as UTC)."""
    now = clock()
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def day_window(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def minute_window(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M")


def dense_usage(days: dict[str, int], today: date) -> dict[str, Any]:
    """``{"today", "last30Days", "days"}``: 30 days, oldest first, the last
    one ``today``, zeros included."""
    entries = []
    for offset in range(USAGE_DAYS - 1, -1, -1):
        key = (today - timedelta(days=offset)).isoformat()
        entries.append({"date": key, "requests": int(days.get(key, 0))})
    return {
        "today": entries[-1]["requests"],
        "last30Days": sum(e["requests"] for e in entries),
        "days": entries,
    }


@dataclass
class PublicApiTokenUsage:
    token_id: str
    name: str
    #: ``{"today", "last30Days", "days"}`` as on the token views.
    usage: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"tokenId": self.token_id, "name": self.name, "usage": self.usage}


@dataclass
class PublicApiAccountUsage:
    #: The account's requests today (UTC), revoked tokens included.
    today: int
    quota: PublicApiQuota
    #: The account's current tokens, oldest first.
    tokens: list[PublicApiTokenUsage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "today": self.today,
            "quota": self.quota.to_dict(),
            "tokens": [t.to_dict() for t in self.tokens],
        }


async def resolve_quota(resolver: QuotaResolver | None, caller: QcliCaller) -> PublicApiQuota:
    """The resolver's quota for ``caller`` (unlimited without a resolver)."""
    if resolver is None:
        return PublicApiQuota()
    quota = resolver(caller)
    if inspect.isawaitable(quota):
        quota = await quota
    if quota is None:
        return PublicApiQuota()
    if not isinstance(quota, PublicApiQuota):
        raise TypeError("the quota resolver must return a PublicApiQuota")
    return quota


class PublicApiUsageService:
    """``public_api.usage``: usage of tokens and accounts."""

    def __init__(
        self,
        users: Any,
        store: Callable[[], IPublicApiUsageStore],
        resolver: Callable[[], QuotaResolver | None],
        clock: Callable[[], datetime],
    ) -> None:
        self._users = users
        self._store = store
        self._resolver = resolver
        self._clock = clock

    async def tokens(self, token_ids: list[str]) -> dict[str, dict[str, Any]]:
        """The token views' ``usage`` (the users plugin's usage provider)."""
        today = utc_now(self._clock).date()
        start = today - timedelta(days=USAGE_DAYS - 1)
        days = await self._store().token_days(list(token_ids), start.isoformat(), today.isoformat())
        return {token_id: dense_usage(days.get(token_id, {}), today) for token_id in token_ids}

    async def account(self, user_id: str) -> PublicApiAccountUsage:
        """The account's requests today, its quota (from the resolver, for a
        caller without a token) and its tokens' usage.  Raises
        ``UsersApiError`` 404 ``user not found``."""
        auth = self._users.auth_service
        user = await auth.user_store.get_user(user_id) if isinstance(user_id, str) and user_id else None
        if user is None or user.id != user_id:
            raise UsersApiError(404, USER_NOT_FOUND)
        permissions = await auth.user_permissions(user)
        caller = QcliCaller(
            user_id=user.id, user_name=user.name, token_id=None, audience=None,
            permissions=tuple(permissions),
        )
        quota = await resolve_quota(self._resolver(), caller)
        now = utc_now(self._clock)
        today = await self._store().account_count(user.id, day_window(now))
        records = await auth.api_token_store.get_tokens(user.id)
        records.sort(key=lambda t: t.created_at)
        usage = await self.tokens([r.id for r in records])
        return PublicApiAccountUsage(
            today=today,
            quota=quota,
            tokens=[PublicApiTokenUsage(r.id, r.name, usage[r.id]) for r in records],
        )
