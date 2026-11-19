"""``usage`` on the token views, through ``users_plugin.set_token_usage_provider``
(registered by the public-api plugin)."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.test_users_host_tokens import BASE, ROOT_PASS, Srv, _make

VIEW_KEYS = ["id", "name", "userId", "userName", "createdAt", "expiresAt", "lastUsedAt", "audience"]


def _usage(offset: int = 0) -> dict:
    today = date(2026, 11, 19)
    days = [
        {"date": (today - timedelta(days=29 - i)).isoformat(), "requests": i + offset}
        for i in range(30)
    ]
    total = sum(d["requests"] for d in days)
    return {"today": days[-1]["requests"], "last30Days": total, "days": days}


@pytest.fixture()
def srv():
    result, users = _make()
    with TestClient(result.app) as client:
        yield Srv(client, users, result)


def _tokens(srv: Srv, session: str, query: str = ""):
    res = srv.c.get(BASE + "/tokens" + query, headers={"Authorization": f"Bearer {session}"})
    assert res.status_code == 200, res.text
    return res.json()


def test_no_provider_no_usage_key(srv: Srv):
    srv.token(srv.alice(), audience="cli")
    session = srv.login("alice", "pass1234")
    views = _tokens(srv, session)
    assert len(views) == 1
    assert list(views[0]) == VIEW_KEYS


def test_provider_adds_usage_last(srv: Srv):
    alice = srv.alice()
    created = srv.token(alice, audience="cli")
    calls: list[list[str]] = []

    async def provider(ids: list[str]) -> dict:
        calls.append(list(ids))
        return {i: _usage() for i in ids}

    srv.users.set_token_usage_provider(provider)
    views = _tokens(srv, srv.login("alice", "pass1234"))
    assert calls == [[created["apiToken"]["id"]]]
    view = views[0]
    assert list(view)[-1] == "usage"
    assert view["usage"]["today"] == 29
    assert view["usage"]["last30Days"] == 435
    assert len(view["usage"]["days"]) == 30
    assert view["usage"]["days"][0] == {"date": "2026-10-21", "requests": 0}


def test_usage_after_permissions(srv: Srv):
    srv.token(srv.alice())  # host token: scoped, so the view has permissions

    async def provider(ids: list[str]) -> dict:
        return {i: _usage() for i in ids}

    srv.users.set_token_usage_provider(provider)
    view = _tokens(srv, srv.login("alice", "pass1234"))[0]
    assert list(view)[-2:] == ["permissions", "usage"]


def test_all_true_calls_the_provider_once_with_every_id(srv: Srv):
    alice = srv.alice()
    a = srv.token(alice, audience="cli", name="a")["apiToken"]["id"]
    b = srv.token(alice, name="b")["apiToken"]["id"]
    calls: list[list[str]] = []

    async def provider(ids: list[str]) -> dict:
        calls.append(sorted(ids))
        return {i: _usage() for i in ids}

    srv.users.set_token_usage_provider(provider)
    root = srv.login("root", ROOT_PASS)
    views = _tokens(srv, root, "?all=true")
    assert calls == [sorted([a, b])]
    assert all("usage" in v for v in views)


def test_provider_failure_keeps_the_list(srv: Srv, caplog):
    srv.token(srv.alice(), audience="cli")

    async def provider(ids: list[str]) -> dict:
        raise RuntimeError("store down")

    srv.users.set_token_usage_provider(provider)
    with caplog.at_level(logging.ERROR):
        views = _tokens(srv, srv.login("alice", "pass1234"))
    assert len(views) == 1 and "usage" not in views[0]
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1


def test_provider_removed(srv: Srv):
    srv.token(srv.alice(), audience="cli")

    async def provider(ids: list[str]) -> dict:
        return {i: _usage() for i in ids}

    srv.users.set_token_usage_provider(provider)
    srv.users.set_token_usage_provider(None)
    assert "usage" not in _tokens(srv, srv.login("alice", "pass1234"))[0]


def test_api_tokens_list_has_usage_create_has_none(srv: Srv):
    alice = srv.alice()

    async def provider(ids: list[str]) -> dict:
        return {i: _usage(1) for i in ids}

    srv.users.set_token_usage_provider(provider)
    created = srv.token(alice)
    assert "usage" not in created["apiToken"]
    listed = srv.call(srv.users.api_tokens.list, alice.id)
    assert listed[0]["usage"]["today"] == 30

    session = srv.login("alice", "pass1234")
    res = srv.c.post(BASE + "/tokens", json={"name": "web"}, headers={"Authorization": f"Bearer {session}"})
    assert res.status_code == 201, res.text
    assert "usage" not in res.json()["apiToken"]


def test_api_tokens_list_provider_failure(srv: Srv):
    alice = srv.alice()
    srv.token(alice)

    async def provider(ids: list[str]) -> dict:
        raise RuntimeError("store down")

    srv.users.set_token_usage_provider(provider)
    listed = srv.call(srv.users.api_tokens.list, alice.id)
    assert len(listed) == 1 and "usage" not in listed[0]
