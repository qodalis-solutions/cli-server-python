"""``api``: the public API usage of an account in the terminal."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from qodalis_cli_server_abstractions import (
    CliCommandExample,
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliModule,
    CliProcessCommand,
    ICliCommandProcessor,
)

from .quota import PublicApiQuota
from .usage_service import USAGE_DAYS, PublicApiAccountUsage, PublicApiUsageService

#: The users plugin's permission to see every user's tokens: needed for another account's usage.
TOKENS_READ_PERMISSION = "tokens:read"


def format_quota(quota: PublicApiQuota) -> str:
    """``1000 per day, 60 per minute``; ``unlimited``; ``no API access`` for a 0 limit."""
    day, minute = quota.requests_per_day, quota.requests_per_minute
    if (day if day is not None else 1) <= 0 or (minute if minute is not None else 1) <= 0:
        return "no API access"
    parts = [f"{n} per {unit}" for n, unit in ((day, "day"), (minute, "minute")) if n is not None]
    return ", ".join(parts) if parts else "unlimited"


def format_account_usage(name: str, usage: PublicApiAccountUsage, days: int) -> str:
    """The account's usage as text: today, the quota, then requests per day (rows) and token (columns) for ``days`` days."""
    lines = [f"{name}: {usage.today} request(s) today (every token, revoked ones too)", f"Quota: {format_quota(usage.quota)}"]
    if not usage.tokens:
        lines.append(f"{name} has no API tokens.")
        return "\n".join(lines)
    dates = [d["date"] for d in usage.tokens[0].usage["days"][-days:]]
    headers = ["Day", *(t.name for t in usage.tokens), "Total"]
    rows: list[list[str]] = []
    for date in dates:
        counts = [next((d["requests"] for d in t.usage["days"] if d["date"] == date), 0) for t in usage.tokens]
        rows.append([date, *(str(c) for c in counts), str(sum(counts))])
    totals = [sum(d["requests"] for d in t.usage["days"][-days:]) for t in usage.tokens]
    rows.append(["Total", *(str(t) for t in totals), str(sum(totals))])
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def line(cells: list[str]) -> str:
        # the day left-aligned, the counts right-aligned
        return "  ".join(c.ljust(widths[i]) if i == 0 else c.rjust(widths[i]) for i, c in enumerate(cells))

    lines.append(line(headers))
    lines.extend(line(r) for r in rows)
    return "\n".join(lines)


def _flag(command: CliProcessCommand, name: str) -> bool:
    value = (command.args or {}).get(name)
    return value is True or (isinstance(value, str) and value.lower() in ("true", "1", ""))


def _caller() -> Any:
    try:
        from qodalis_cli import get_cli_caller
    except ImportError:  # pragma: no cover - the plugin without the server package
        return None
    return get_cli_caller()


class ApiUsageProcessor(CliCommandProcessor):
    command = "usage"
    description = "Shows the public API requests of an account per token and day, with its quota (your own unless --user)"
    parameters = [
        CliCommandParameterDescriptor("user", f"Another account, by name or id (needs {TOKENS_READ_PERMISSION})", False, "string"),
        CliCommandParameterDescriptor(
            "days", f"How many days back, today included (1-{USAGE_DAYS}, default 7)", False, "number", default_value=7,
        ),
        CliCommandParameterDescriptor("json", "Print the usage as JSON", False, "boolean"),
    ]
    examples = [
        CliCommandExample("api usage", "Your requests of the last 7 days"),
        CliCommandExample("api usage --user alice --days 30", "alice's requests of the last 30 days"),
    ]

    def __init__(self, usage: PublicApiUsageService, users: Any) -> None:
        self._usage = usage
        self._users = users

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        args = command.args or {}
        raw_days = args.get("days")
        days: Any
        if raw_days is None:
            days = 7
        else:
            try:
                days = float(raw_days) if raw_days is not True else None
            except (TypeError, ValueError):
                days = None
        if days is None or not float(days).is_integer() or not 1 <= days <= USAGE_DAYS:
            return f"Error: --days must be a whole number from 1 to {USAGE_DAYS}"
        days = int(days)
        caller = _caller()
        user_arg = args.get("user")
        if user_arg is True or (user_arg is not None and str(user_arg).strip() == ""):
            return "Error: --user needs a name or id"
        if user_arg is None:
            if caller is None:
                return "Error: no signed-in user: name the account with --user <name|id>"
            user_id, user_name = caller.user_id, caller.user_name
        else:
            key = str(user_arg).strip()
            user = await self._users.auth_service.user_store.get_user(key)
            if user is None:
                return f"Error: user not found: {key}"
            user_id, user_name = user.id, user.name
            if caller is not None and user_id != caller.user_id:
                from qodalis_cli_users.permissions import has_permission

                if not has_permission(caller.permissions, TOKENS_READ_PERMISSION):
                    return f"Error: another account's usage needs {TOKENS_READ_PERMISSION}"
        try:
            account = await self._usage.account(user_id)
        except Exception as exc:  # noqa: BLE001 - UsersApiError (404 user not found)
            detail = getattr(exc, "detail", None)
            if detail is None:
                raise
            return f"Error: {detail}"
        if _flag(command, "json"):
            tokens = []
            for t in account.tokens:
                view = t.to_dict()
                view["usage"] = {**view["usage"], "days": view["usage"]["days"][-days:]}
                tokens.append(view)
            return json.dumps({
                "user": {"id": user_id, "name": user_name}, "days": days, "today": account.today,
                "quota": account.quota.to_dict(), "tokens": tokens,
            }, indent=2)
        return format_account_usage(user_name, account, days)


class CliApiCommandProcessor(CliCommandProcessor):
    """``api`` (= ``api usage``), ``api usage [--user <name|id>] [--days N] [--json]``: the public API usage of an
    account.  Your own with commands:execute; another account's with ``tokens:read``, like ``GET /tokens?all=true``."""

    command = "api"
    description = "Public API: requests per token and day, and the quota"
    module = "public-api"
    icon = "🔌"

    def __init__(self, usage: PublicApiUsageService, users: Any) -> None:
        self._usage_processor = ApiUsageProcessor(usage, users)
        self._processors: list[ICliCommandProcessor] = [self._usage_processor]

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return self._processors

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return await self._usage_processor.handle_async(command, cancellation_event)


class PublicApiModule(CliModule):
    """The ``api usage`` terminal command, registered by ``mount_plugin``."""

    name = "public-api"
    description = "Public API usage per token and day, and the quota"
    icon = "🔌"

    def __init__(self, processor: CliApiCommandProcessor, version: str) -> None:
        self._processor = processor
        self._version = version

    @property
    def version(self) -> str:  # type: ignore[override]
        return self._version

    @property
    def processors(self) -> list[ICliCommandProcessor]:  # type: ignore[override]
        return [self._processor]
