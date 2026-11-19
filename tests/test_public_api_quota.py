"""The public-api guard: host tokens, permission, per-account daily and
per-minute quotas, ``X-RateLimit-*`` headers and token usage (contract C5)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import httpx
import pytest
from fastapi import APIRouter, Depends, HTTPException
from fastapi.testclient import TestClient

from qodalis_cli_public_api import (
    CliPublicApiBuilder,
    InMemoryPublicApiUsageStore,
    PublicApiQuota,
)
from qodalis_cli_users import ApiTokenCreateOptions, QcliCaller, User, UsersApiError

from tests.test_users_host_tokens import BASE, _make as _make_users

START = datetime(2026, 11, 19, 23, 59, 30, tzinfo=timezone.utc)
TODAY = "2026-11-19"


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Host:
    def __init__(self, quota: Any = None, store: Any = None) -> None:
        self.clock = Clock()
        self.result, self.users = _make_users()
        builder = CliPublicApiBuilder().set_users(self.users).set_clock(self.clock)
        if quota is not None:
            builder.set_quota(quota)
        if store is not None:
            builder.set_usage_store(store)
        self.api = builder.build()
        self.result.mount_plugin(self.api)
        api = self.api

        @self.result.app.get("/v1/rates")
        async def rates(caller=Depends(api.guard("rates:read"))):
            return {"tokenId": caller.token_id}

        @self.result.app.get("/v1/open")
        async def open_route(caller=Depends(api.guard())):
            return {"ok": True}

        @self.result.app.get("/v1/missing")
        async def missing(caller=Depends(api.guard("rates:read"))):
            raise HTTPException(status_code=404, detail="no such rate")

    @property
    def app(self) -> Any:
        return self.result.app


class Srv:
    def __init__(self, host: Host, client: TestClient) -> None:
        self.host = host
        self.c = client
        self.users = host.users
        self.api = host.api
        self.clock = host.clock

    def call(self, fn, *args):
        return self.c.portal.call(fn, *args)

    def user(self, name: str, perms: list[str] | None = None) -> User:
        async def make() -> User:
            await self.users.ensure_seeded()
            await self.users.group_store.create_group(f"{name}-g", permissions=perms or ["rates:read"])
            user = await self.users.user_store.create_user(name, f"{name}@example.com", ["users", f"{name}-g"])
            await self.users.auth_service.set_password(user.id, "pass1234")
            return user

        return self.call(make)

    def token(self, user: User, *, name: str = "t", audience: str = "host", permissions: list[str] | None = None) -> dict:
        perms = permissions if permissions is not None else (["rates:read"] if audience == "host" else None)
        return self.call(
            self.users.api_tokens.create, user.id,
            ApiTokenCreateOptions(name=name, audience=audience, permissions=perms),
        ).to_dict()

    def host_token(self, user_name: str = "alice") -> tuple[User, str, str]:
        user = self.user(user_name)
        created = self.token(user)
        return user, created["token"], created["apiToken"]["id"]

    def get(self, path: str, token: str | None):
        return self.c.get(path, headers={"Authorization": f"Bearer {token}"} if token else {})

    def login(self, name: str) -> str:
        res = self.c.post(BASE + "/sessions", json={"username": name, "password": "pass1234"})
        assert res.status_code == 201, res.text
        return res.json()["token"]

    def account_count(self, user_id: str, window: str = TODAY) -> int:
        return self.call(self.api.store.account_count, user_id, window)


@contextmanager
def _srv(quota: Any = None, store: Any = None) -> Iterator[Srv]:
    host = Host(quota, store)
    with TestClient(host.app) as client:
        yield Srv(host, client)


def _limit(day: int | None, minute: int | None = None):
    return lambda caller: PublicApiQuota(requests_per_day=day, requests_per_minute=minute)


@pytest.fixture()
def srv():
    with _srv() as s:
        yield s


def _rate_headers(res: httpx.Response) -> dict[str, str]:
    return {k.lower(): v for k, v in res.headers.items() if k.lower().startswith("x-ratelimit") or k.lower() == "retry-after"}


def test_unlimited_no_headers(srv: Srv):
    _, token, token_id = srv.host_token()
    res = srv.get("/v1/rates", token)
    assert res.status_code == 200, res.text
    assert res.json() == {"tokenId": token_id}
    assert _rate_headers(res) == {}


def test_refusals(srv: Srv):
    alice, host, _ = srv.host_token()
    cli = srv.token(alice, name="cli", audience="cli")["token"]
    session = srv.login("alice")
    carol = srv.user("carol", ["rates:read", "banks:read"])
    no_rates = srv.token(carol, permissions=["banks:read"])["token"]

    cases = [
        (None, "/v1/rates", 401, "unauthorized"),
        ("qcli_pat_unknown", "/v1/rates", 401, "invalid or expired token"),
        (session, "/v1/rates", 403, "forbidden: a host token is required"),
        (cli, "/v1/rates", 403, "forbidden: a host token is required"),
        (no_rates, "/v1/rates", 403, "forbidden: missing permission rates:read"),
    ]
    for token, path, status, text in cases:
        res = srv.get(path, token)
        assert (res.status_code, res.json()) == (status, {"error": text}), (token, path)
        assert _rate_headers(res) == {}
    assert srv.get("/v1/open", no_rates).status_code == 200
    assert srv.account_count(alice.id) == 0
    assert srv.account_count(carol.id) == 1  # the /v1/open request only


def test_refusals_with_limits_send_no_headers():
    with _srv(_limit(3)) as srv:
        res = srv.get("/v1/rates", None)
        assert res.status_code == 401 and _rate_headers(res) == {}


def test_daily_headers_and_limit():
    with _srv(_limit(3)) as srv:
        _, token, _ = srv.host_token()
        for remaining in ("2", "1", "0"):
            res = srv.get("/v1/rates", token)
            assert res.status_code == 200, res.text
            assert _rate_headers(res) == {
                "x-ratelimit-limit": "3", "x-ratelimit-remaining": remaining, "x-ratelimit-reset": "30",
            }
        res = srv.get("/v1/rates", token)
        assert res.status_code == 429
        assert res.json() == {"error": "quota exceeded: 3 requests per day"}
        assert res.headers["retry-after"] == "30"
        assert res.headers["x-ratelimit-remaining"] == "0"
        assert res.headers["x-ratelimit-limit"] == "3"


def test_minute_limit():
    with _srv(_limit(None, 2)) as srv:
        _, token, _ = srv.host_token()
        assert srv.get("/v1/rates", token).status_code == 200
        assert srv.get("/v1/rates", token).headers["x-ratelimit-remaining"] == "0"
        res = srv.get("/v1/rates", token)
        assert res.status_code == 429
        assert res.json() == {"error": "too many requests: 2 per minute"}
        assert res.headers["retry-after"] == "30"
        assert res.headers["x-ratelimit-limit"] == "2"
        assert res.headers["x-ratelimit-reset"] == "30"


def test_both_limits_headers_describe_the_day():
    with _srv(_limit(100, 2)) as srv:
        _, token, _ = srv.host_token()
        res = srv.get("/v1/rates", token)
        assert res.headers["x-ratelimit-limit"] == "100"
        assert res.headers["x-ratelimit-remaining"] == "99"
        srv.get("/v1/rates", token)
        res = srv.get("/v1/rates", token)
        assert res.status_code == 429
        assert res.json() == {"error": "too many requests: 2 per minute"}
        assert res.headers["x-ratelimit-limit"] == "100"
        assert res.headers["x-ratelimit-remaining"] == "0"
        assert res.headers["retry-after"] == "30"


@pytest.mark.parametrize("quota", [(0, None), (-5, None), (None, 0), (10, -1)])
def test_zero_limit_returns_403(quota):
    with _srv(_limit(*quota)) as srv:
        alice, token, _ = srv.host_token()
        res = srv.get("/v1/rates", token)
        assert (res.status_code, res.json()) == (403, {"error": "forbidden: no API access"})
        assert _rate_headers(res) == {}
        assert srv.account_count(alice.id) == 0


def test_quota_is_per_account():
    with _srv(_limit(3)) as srv:
        alice = srv.user("alice")
        first = srv.token(alice, name="a")["token"]
        second = srv.token(alice, name="b")["token"]
        assert srv.get("/v1/rates", first).status_code == 200
        assert srv.get("/v1/rates", first).status_code == 200
        res = srv.get("/v1/rates", second)
        assert res.status_code == 200 and res.headers["x-ratelimit-remaining"] == "0"
        assert srv.get("/v1/rates", second).status_code == 429
        assert srv.get("/v1/rates", first).status_code == 429
        _, other, _ = srv.host_token("bob")
        assert srv.get("/v1/rates", other).status_code == 200


def test_quota_revoked_tokens_still_count():
    with _srv(_limit(2)) as srv:
        alice = srv.user("alice")
        a = srv.token(alice, name="a")
        assert srv.get("/v1/rates", a["token"]).status_code == 200
        assert srv.get("/v1/rates", a["token"]).status_code == 200
        assert srv.call(srv.users.api_tokens.revoke, alice.id, a["apiToken"]["id"]) is True
        b = srv.token(alice, name="b")
        res = srv.get("/v1/rates", b["token"])
        assert res.status_code == 429
        assert res.json() == {"error": "quota exceeded: 2 requests per day"}


def test_quota_day_rolls_over_at_utc_midnight():
    with _srv(_limit(1)) as srv:
        _, token, _ = srv.host_token()
        assert srv.get("/v1/rates", token).status_code == 200
        res = srv.get("/v1/rates", token)
        assert res.status_code == 429 and res.headers["retry-after"] == "30"
        srv.clock.advance(31)  # 00:00:01Z, a new UTC day
        res = srv.get("/v1/rates", token)
        assert res.status_code == 200, res.text
        assert res.headers["x-ratelimit-reset"] == str(86400 - 1)
        assert res.headers["x-ratelimit-remaining"] == "0"


def test_clock_in_another_time_zone_is_read_in_utc():
    with _srv(_limit(1)) as srv:
        _, token, _ = srv.host_token()
        # 23:59:30Z seen from UTC+2 is already 01:59:30 on the next day.
        srv.clock.now = START.astimezone(timezone(timedelta(hours=2)))
        res = srv.get("/v1/rates", token)
        assert res.headers["x-ratelimit-reset"] == "30"
        assert srv.account_count(srv.call(srv.users.user_store.get_user, "alice").id, TODAY) == 1


def test_minute_window_resets():
    with _srv(_limit(None, 1)) as srv:
        _, token, _ = srv.host_token()
        assert srv.get("/v1/rates", token).status_code == 200
        assert srv.get("/v1/rates", token).status_code == 429
        srv.clock.advance(30)
        assert srv.get("/v1/rates", token).status_code == 200


class _YieldingStore(InMemoryPublicApiUsageStore):
    """Yields to the loop in every call, as a store over a database does."""

    async def increment(self, token_id: str, user_id: str, window: str) -> int:
        await asyncio.sleep(0)
        return await super().increment(token_id, user_id, window)

    async def decrement(self, token_id: str, window: str) -> None:
        await asyncio.sleep(0)
        await super().decrement(token_id, window)

    async def account_count(self, user_id: str, window: str) -> int:
        await asyncio.sleep(0)
        return await super().account_count(user_id, window)


@pytest.mark.parametrize("store", [InMemoryPublicApiUsageStore, _YieldingStore])
async def test_quota_parallel_requests_admit_exactly_the_limit(store):
    host = Host(_limit(10), store())
    users = host.users
    await users.ensure_seeded()
    await users.group_store.create_group("alice-g", permissions=["rates:read"])
    alice = await users.user_store.create_user("alice", "alice@example.com", ["users", "alice-g"])
    created = await users.api_tokens.create(
        alice.id, ApiTokenCreateOptions(name="t", audience="host", permissions=["rates:read"]),
    )
    headers = {"Authorization": f"Bearer {created.token}"}
    transport = httpx.ASGITransport(app=host.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        responses = await asyncio.gather(*(client.get("/v1/rates", headers=headers) for _ in range(50)))
    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 10
    assert statuses.count(429) == 40
    assert await host.api.store.account_count(alice.id, TODAY) == 10


def test_quota_resolver_throws_returns_500_not_counted(caplog):
    state = {"fail": True}

    def resolver(caller: QcliCaller) -> PublicApiQuota:
        if state["fail"]:
            raise RuntimeError("db down")
        return PublicApiQuota(requests_per_day=3)

    with _srv(resolver) as srv:
        alice, token, _ = srv.host_token()
        with caplog.at_level(logging.ERROR):
            res = srv.get("/v1/rates", token)
        assert res.status_code == 500
        assert res.json() == {"error": "internal server error"}
        assert "db down" not in res.text
        assert _rate_headers(res) == {}
        assert any("db down" in (r.exc_text or "") or "db down" in str(r.exc_info) for r in caplog.records)
        assert srv.account_count(alice.id) == 0
        state["fail"] = False
        res = srv.get("/v1/rates", token)
        assert res.status_code == 200
        assert res.headers["x-ratelimit-remaining"] == "2"


def test_resolver_gets_the_caller():
    seen: list[QcliCaller] = []

    def resolver(caller: QcliCaller) -> PublicApiQuota:
        seen.append(caller)
        return PublicApiQuota()

    with _srv(resolver) as srv:
        alice, token, token_id = srv.host_token()
        assert srv.get("/v1/rates", token).status_code == 200
        assert len(seen) == 1
        assert (seen[0].user_id, seen[0].token_id, seen[0].audience) == (alice.id, token_id, "host")
        assert seen[0].user_name == "alice"
        assert seen[0].permissions == ("rates:read",)


def test_async_resolver():
    async def resolver(caller: QcliCaller) -> PublicApiQuota:
        await asyncio.sleep(0)
        return PublicApiQuota(requests_per_day=5)

    with _srv(resolver) as srv:
        _, token, _ = srv.host_token()
        res = srv.get("/v1/rates", token)
        assert res.status_code == 200
        assert res.headers["x-ratelimit-limit"] == "5"


def test_usage_on_token_views():
    with _srv(_limit(3)) as srv:
        alice = srv.user("alice")
        a = srv.token(alice, name="a")
        b = srv.token(alice, name="b")
        for _ in range(3):
            assert srv.get("/v1/rates", a["token"]).status_code == 200
        session = srv.login("alice")
        views = srv.c.get(BASE + "/tokens", headers={"Authorization": f"Bearer {session}"}).json()
        by_id = {v["id"]: v for v in views}
        usage_a = by_id[a["apiToken"]["id"]]["usage"]
        assert list(by_id[a["apiToken"]["id"]])[-1] == "usage"
        assert usage_a["today"] == 3 and usage_a["last30Days"] == 3
        assert len(usage_a["days"]) == 30
        assert usage_a["days"][-1] == {"date": TODAY, "requests": 3}
        assert usage_a["days"][0] == {"date": "2026-10-21", "requests": 0}
        assert by_id[b["apiToken"]["id"]]["usage"]["today"] == 0

        account = srv.call(srv.api.usage.account, alice.id)
        assert account.today == 3
        assert account.quota == PublicApiQuota(requests_per_day=3, requests_per_minute=None)
        assert [t.name for t in account.tokens] == ["a", "b"]
        assert account.tokens[0].usage["today"] == 3
        assert account.to_dict()["quota"] == {"requestsPerDay": 3, "requestsPerMinute": None}
        assert list(account.to_dict()) == ["today", "quota", "tokens"]
        assert list(account.to_dict()["tokens"][0]) == ["tokenId", "name", "usage"]


def test_usage_unknown_user_throws_404(srv: Srv):
    with pytest.raises(UsersApiError) as exc:
        srv.call(srv.api.usage.account, "nobody")
    assert (exc.value.status_code, exc.value.message) == (404, "user not found")


def test_headers_on_handler_errors():
    with _srv(_limit(3)) as srv:
        _, token, _ = srv.host_token()
        res = srv.get("/v1/missing", token)
        assert res.status_code == 404
        assert res.headers["x-ratelimit-limit"] == "3"
        assert res.headers["x-ratelimit-remaining"] == "2"


def test_prune_once_an_hour():
    store = InMemoryPublicApiUsageStore()
    with _srv(_limit(100), store) as srv:
        _, token, _ = srv.host_token()
        assert srv.get("/v1/rates", token).status_code == 200
        srv.clock.advance(40 * 24 * 3600)
        assert srv.get("/v1/rates", token).status_code == 200
        windows = {w for (_, w) in store._counts}
        assert all(not w.startswith("2026-11") for w in windows), windows


def test_build_requires_users():
    with pytest.raises(ValueError, match=r"public-api: set_users\(users_plugin\) is required"):
        CliPublicApiBuilder().build()


def test_portal_section_and_host_routes():
    api = CliPublicApiBuilder().set_users(_make_users()[1]).set_title("Rates API").build()
    assert api.portal_section == {"kind": "api", "title": "Rates API", "base_path": "/qcli/api"}
    assert api.cli_routes is False
    assert api.prefix == "/qcli/api"
    assert api.guard("rates:read").qcli_host_token_only is True
    assert api.guard("rates:read").qcli_permission == "rates:read"


async def test_in_memory_store():
    store = InMemoryPublicApiUsageStore()
    assert await store.increment("t1", "u1", TODAY) == 1
    assert await store.increment("t1", "u1", TODAY) == 2
    await store.increment("t2", "u1", TODAY)
    await store.increment("t3", "u2", TODAY)
    assert await store.account_count("u1", TODAY) == 3
    await store.decrement("t1", TODAY)
    assert await store.account_count("u1", TODAY) == 2
    await store.increment("t1", "u1", "2026-11-18")
    await store.increment("t1", "u1", "2026-11-17")
    await store.increment("t1", "u1", "2026-11-19T10:00")
    days = await store.token_days(["t1", "t9"], "2026-11-18", TODAY)
    assert days == {"t1": {"2026-11-18": 1, TODAY: 1}}
    await store.prune("2026-11-18", "2026-11-19T10:01")
    assert await store.account_count("u1", "2026-11-17") == 0
    assert await store.account_count("u1", "2026-11-19T10:00") == 0
    assert await store.account_count("u1", "2026-11-18") == 1


class _FailingStore(InMemoryPublicApiUsageStore):
    """Raises on the minute increment or on the account-count read."""

    def __init__(self, fail_on: str) -> None:
        super().__init__()
        self.fail_on = fail_on
        self.failing = True

    async def increment(self, token_id: str, user_id: str, window: str) -> int:
        if self.failing and self.fail_on == "minute" and len(window) == 16:
            raise RuntimeError("store down")
        return await super().increment(token_id, user_id, window)

    async def account_count(self, user_id: str, window: str) -> int:
        if self.failing and self.fail_on == "count":
            raise RuntimeError("store down")
        return await super().account_count(user_id, window)


@pytest.mark.parametrize("fail_on", ["minute", "count"])
def test_store_failure_returns_500_and_takes_back_the_counts(fail_on: str):
    store = _FailingStore(fail_on)
    with _srv(_limit(3, 10), store) as srv:
        alice, token, token_id = srv.host_token()
        res = srv.get("/v1/rates", token)
        assert (res.status_code, res.json()) == (500, {"error": "internal server error"})
        assert "store down" not in res.text
        store.failing = False
        assert srv.account_count(alice.id) == 0
        assert srv.account_count(alice.id, "2026-11-19T23:59") == 0
        res = srv.get("/v1/rates", token)
        assert res.status_code == 200 and res.headers["x-ratelimit-remaining"] == "2"


def test_store_failure_without_limits_returns_500():
    store = _FailingStore("minute")
    with _srv(None, store) as srv:
        alice, token, _ = srv.host_token()
        assert srv.get("/v1/rates", token).status_code == 500
        store.failing = False
        assert srv.account_count(alice.id) == 0


def test_stacked_guards_count_once():
    with _srv(_limit(2)) as srv:
        api = srv.api
        router = APIRouter(dependencies=[Depends(api.guard())])

        @router.get("/v2/rates")
        async def rates(caller=Depends(api.guard("rates:read"))):
            return {"ok": True}

        @router.get("/v2/banks")
        async def banks(caller=Depends(api.guard("banks:read"))):
            return {"ok": True}

        srv.host.app.include_router(router)
        alice, token, _ = srv.host_token()
        first = srv.get("/v2/rates", token)
        assert first.status_code == 200 and first.headers["x-ratelimit-remaining"] == "1"
        assert srv.get("/v2/rates", token).status_code == 200
        res = srv.get("/v2/rates", token)
        assert (res.status_code, res.json()) == (429, {"error": "quota exceeded: 2 requests per day"})
        assert srv.account_count(alice.id) == 2
        assert srv.call(srv.api.usage.account, alice.id).today == 2


def test_stacked_guard_missing_permission():
    with _srv(_limit(5)) as srv:
        api = srv.api
        router = APIRouter(dependencies=[Depends(api.guard())])

        @router.get("/v2/banks")
        async def banks(caller=Depends(api.guard("banks:read"))):
            return {"ok": True}

        srv.host.app.include_router(router)
        alice, token, _ = srv.host_token()
        res = srv.get("/v2/banks", token)
        assert (res.status_code, res.json()) == (403, {"error": "forbidden: missing permission banks:read"})
        assert _rate_headers(res) == {}
        # Refused by the route's guard before the router's guard counted it.
        assert srv.account_count(alice.id) == 0


def test_stacked_guards_most_specific_permission_in_the_document():
    with _srv() as srv:
        api = srv.api
        router = APIRouter(dependencies=[Depends(api.guard("rates:read"))])

        @router.get("/v2/banks")
        async def banks(caller=Depends(api.guard("banks:read"))):
            return {}

        from qodalis_cli_public_api.openapi import _guard_tags

        route = next(r for r in router.routes if r.path == "/v2/banks")
        assert _guard_tags(route.dependant) == ("banks:read", True)


class _BlockingStore(InMemoryPublicApiUsageStore):
    """Holds the minute increment until released (waiting on a slow database)."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def increment(self, token_id: str, user_id: str, window: str) -> int:
        if len(window) == 16:
            self.entered.set()
            await self.release.wait()
        return await super().increment(token_id, user_id, window)


async def test_cancelled_admission_is_not_counted():
    store = _BlockingStore()
    host = Host(_limit(5), store)
    caller = QcliCaller(user_id="u1", user_name="u", token_id="t1", audience="host", permissions=("rates:read",))
    task = asyncio.ensure_future(host.api._admit(caller))
    await store.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await store.account_count("u1", TODAY) == 0
    assert await store.account_count("u1", "2026-11-19T23:59") == 0
