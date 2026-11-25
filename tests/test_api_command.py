"""The public-api plugin's ``api usage`` terminal command through POST /execute: your own usage per token and day
with the quota, --days, --json, another account by name or id (tokens:read), no tokens, unknown users and bad
arguments."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

from fastapi.testclient import TestClient

from qodalis_cli import create_cli_server
from qodalis_cli_public_api import CliPublicApiBuilder, InMemoryPublicApiUsageStore, PublicApiQuota, format_quota
from qodalis_cli_users import ApiTokenCreateOptions

from .command_helpers import add_users, protect, run_command

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


class Env:
    def __init__(self, client: TestClient, result: Any, users: Any, store: Any, tokens: dict[str, str]) -> None:
        self.client, self.result, self.users, self.store, self.tokens = client, result, users, store, tokens

    def run(self, line: str, user: Optional[str] = None):
        return run_command(self.client, self.result, line, self.tokens[user] if user else None)

    def text(self, line: str, user: Optional[str] = None) -> str:
        return self.run(line, user).text

    def token_with_usage(self, user_name: str, name: str, requests: dict[str, int]) -> tuple[Any, str]:
        """A host token of the user and ``requests`` counted on each of the days."""
        async def create() -> tuple[Any, str]:
            user = await self.users.auth_service.user_store.get_user(user_name)
            created = await self.users.api_tokens.create(
                user.id, ApiTokenCreateOptions(name=name, audience="host", permissions=["rates:read"]),
            )
            token_id = created.api_token["id"]
            for day, n in requests.items():
                for _ in range(n):
                    await self.store.increment(token_id, user.id, day)
            return user, token_id
        return self.client.portal.call(create)


@contextmanager
def start(quota: Callable[[str], PublicApiQuota] | None = None) -> Iterator[Env]:
    result = create_cli_server()
    result.builder.add_permissions([{"name": "rates:read", "description": "Rates", "group": "Rates"}])
    users = protect(result)
    store = InMemoryPublicApiUsageStore()
    builder = CliPublicApiBuilder().set_users(users).set_usage_store(store).set_clock(lambda: NOW)
    if quota is not None:
        builder.set_quota(lambda caller: quota(caller.user_name))
    result.mount_plugin(builder.build())
    with TestClient(result.app) as client:
        tokens = add_users(client, {
            "alice": ["commands:execute", "rates:read"],
            "auditor": ["commands:execute", "tokens:read"],
        })
        yield Env(client, result, users, store, tokens)


def test_registered_as_api_with_usage_needing_no_permission_beyond_commands_execute():
    with start() as env:
        root = env.result.registry.find_processor("api")
        assert root.required_permissions is None
        assert [(p.command, p.required_permissions) for p in root.processors] == [("usage", None)]


def test_api_usage_your_requests_per_day_and_token_totals_today_and_the_quota():
    with start(lambda _: PublicApiQuota(requests_per_day=1000, requests_per_minute=60)) as env:
        env.token_with_usage("alice", "website", {"2026-11-25": 3, "2026-11-24": 2, "2026-11-10": 9})
        env.token_with_usage("alice", "ci-bot", {"2026-11-25": 10})
        assert env.text("api usage --days 3", "alice") == "\n".join([
            "alice: 13 request(s) today (every token, revoked ones too)",
            "Quota: 1000 per day, 60 per minute",
            "Day         website  ci-bot  Total",
            "2026-11-23        0       0      0",
            "2026-11-24        2       0      2",
            "2026-11-25        3      10     13",
            "Total             5      10     15",
        ])
        # api alone is api usage, 7 days by default
        assert len(env.text("api", "alice").split("\n")) == 2 + 1 + 7 + 1
        assert "Total            14      10     24" in env.text("api usage --days 30", "alice")


def test_json_the_account_usage_with_the_days_asked_for():
    with start() as env:
        user, token_id = env.token_with_usage("alice", "website", {"2026-11-25": 4})
        body = json.loads(env.text("api usage --days 2 --json", "alice"))
        assert body == {
            "user": {"id": user.id, "name": "alice"},
            "days": 2,
            "today": 4,
            "quota": {"requestsPerDay": None, "requestsPerMinute": None},
            "tokens": [{
                "tokenId": token_id,
                "name": "website",
                "usage": {
                    "today": 4, "last30Days": 4,
                    "days": [{"date": "2026-11-24", "requests": 0}, {"date": "2026-11-25", "requests": 4}],
                },
            }],
        }


def test_an_account_without_tokens_unlimited():
    with start() as env:
        assert env.text("api usage", "alice") == "\n".join([
            "alice: 0 request(s) today (every token, revoked ones too)", "Quota: unlimited", "alice has no API tokens.",
        ])


def test_another_accounts_usage_needs_tokens_read_by_name_or_id():
    with start() as env:
        user, _ = env.token_with_usage("alice", "website", {"2026-11-25": 1})
        assert env.text("api usage --user auditor", "alice") == "Error: another account's usage needs tokens:read"
        assert "alice: 1 request(s) today" in env.text("api usage --user alice", "auditor")
        assert "alice: 1 request(s) today" in env.text(f"api usage --user {user.id}", "auditor")
        assert "alice: 1 request(s) today" in env.text("api usage --user ALICE", "root")
        # naming yourself needs nothing more
        assert "alice: 1 request(s) today" in env.text("api usage --user alice", "alice")


def test_refuses_unknown_users_and_bad_arguments():
    with start() as env:
        assert env.text("api usage --user nobody", "root") == "Error: user not found: nobody"
        assert env.text("api usage --user", "root") == "Error: --user needs a name or id"
        for bad in ["0", "31", "x", "2.5"]:
            assert env.text(f"api usage --days {bad}", "root") == "Error: --days must be a whole number from 1 to 30"


def test_the_quota_as_the_resolver_gives_it_for_the_account():
    def quota(name: str) -> PublicApiQuota:
        if name == "alice":
            return PublicApiQuota(requests_per_day=0)
        return PublicApiQuota(requests_per_minute=5)

    with start(quota) as env:
        assert "Quota: no API access" in env.text("api usage", "alice")
        assert "Quota: 5 per minute" in env.text("api usage", "auditor")


def test_anonymous_callers_are_refused():
    with start() as env:
        assert env.run("api usage").status == 401


def test_format_quota():
    assert format_quota(PublicApiQuota()) == "unlimited"
    assert format_quota(PublicApiQuota(requests_per_day=10)) == "10 per day"
    assert format_quota(PublicApiQuota(requests_per_day=10, requests_per_minute=-1)) == "no API access"
