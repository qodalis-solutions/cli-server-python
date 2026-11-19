"""Quotas of the public API: the limits a host returns for a caller."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Union

from qodalis_cli_users import QcliCaller


@dataclass
class PublicApiQuota:
    """The limits of an account: requests per UTC day and per UTC minute,
    counted over every token of the account (revoked ones included).
    ``None`` = unlimited; ``0`` (or a negative value) = no API access."""

    requests_per_day: int | None = None
    requests_per_minute: int | None = None

    def to_dict(self) -> dict[str, int | None]:
        return {"requestsPerDay": self.requests_per_day, "requestsPerMinute": self.requests_per_minute}


#: ``(caller) -> PublicApiQuota``, sync or async (``set_quota``).  Called on
#: every guarded request; an exception answers 500 ``internal server error``.
QuotaResolver = Callable[[QcliCaller], Union[PublicApiQuota, Awaitable[PublicApiQuota]]]
