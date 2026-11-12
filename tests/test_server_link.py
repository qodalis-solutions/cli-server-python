"""Link requests: a terminal asks to be linked, a signed-in user approves it
in a browser (spec: web-cli docs/superpowers/specs/2026-11-12-server-linking-design.md)."""

from __future__ import annotations

import itertools
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import CliEchoCommandProcessor, CliServerOptions, create_cli_server
from qodalis_cli_users import CliUsersBuilder
from qodalis_cli_users.errors import UsersApiError
from qodalis_cli_users.services.link_service import (
    LINK_EXPIRES_IN_SECONDS,
    LinkService,
    link_token_name,
    link_verification_url,
    normalize_user_code,
)

BASE = "/api/v1/qcli"
ROOT_PASS = "rootpass1"
ORIGIN = "https://cli.qodalis.com"
CLIENT = "Qodalis CLI on Pixel 7"


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# =========================================================================
# Helpers
# =========================================================================


class TestHelpers:
    def test_token_names(self):
        assert link_token_name("Qodalis CLI on Pixel 7") == "link Qodalis CLI on Pixel 7"
        assert link_token_name("café/terminal") == "link caf--terminal"
        assert len(link_token_name("x" * 100)) == 64
        assert link_token_name("Pixel", 1) == "link Pixel 2"

    def test_verification_url(self):
        assert link_verification_url("/qcli/admin/link", "ABCD-2345") == "/qcli/admin/link?code=ABCD-2345"
        assert link_verification_url("https://app.example.com/link/{code}", "ABCD-2345") == \
            "https://app.example.com/link/ABCD-2345"
        assert link_verification_url("/approve?x=1", "ABCD-2345") == "/approve?x=1&code=ABCD-2345"

    def test_normalises_codes(self):
        assert normalize_user_code(" abcd-2345 ") == "ABCD2345"

    def test_expires_after_10_minutes(self):
        now = [1_000_000]
        links = LinkService(None, "/qcli/admin/link", now=lambda: now[0])  # type: ignore[arg-type]
        created = links.create({"client": "term"})
        assert links.poll(created["linkId"]) == {"status": "pending"}
        now[0] += LINK_EXPIRES_IN_SECONDS * 1000
        assert links.poll(created["linkId"]) is None
        with pytest.raises(UsersApiError) as exc:
            links.view(created["userCode"])
        assert exc.value.status_code == 404
        assert "not found" in exc.value.message


class TestParallelApprovals:
    async def test_second_approval_does_not_create_a_token(self, monkeypatch):
        import asyncio
        from types import SimpleNamespace

        from qodalis_cli_users.controllers import tokens_controller

        calls: list[str] = []

        async def slow_create(_auth, _ctx, name, _perms, expires_at):
            calls.append(name)
            await asyncio.sleep(0.01)
            return "qcli_pat_x", SimpleNamespace(id=f"t{len(calls)}", name=name, expires_at=expires_at)

        async def audit(*_args, **_kwargs):
            return None

        monkeypatch.setattr(tokens_controller, "create_api_token", slow_create)
        links = LinkService(SimpleNamespace(audit=audit), "/qcli/admin/link")  # type: ignore[arg-type]
        created = links.create({"client": "term"})
        ctx = SimpleNamespace(api_token=None, session=object(), external=False,
                              user=SimpleNamespace(name="root", id="u1"))
        results = await asyncio.gather(
            links.approve(ctx, created["userCode"]),  # type: ignore[arg-type]
            links.approve(ctx, created["userCode"]),  # type: ignore[arg-type]
            return_exceptions=True,
        )
        assert len(calls) == 1
        assert sum(isinstance(r, UsersApiError) and r.status_code == 404 for r in results) == 1
        assert links.poll(created["linkId"])["status"] == "approved"

    async def test_failed_approval_leaves_the_request_pending(self, monkeypatch):
        from types import SimpleNamespace

        from qodalis_cli_users.controllers import tokens_controller

        async def failing_create(*_args):
            raise UsersApiError(400, "token limit reached")

        monkeypatch.setattr(tokens_controller, "create_api_token", failing_create)
        links = LinkService(None, "/qcli/admin/link")  # type: ignore[arg-type]
        created = links.create({"client": "term"})
        ctx = SimpleNamespace(api_token=None, session=object(), external=False)
        with pytest.raises(UsersApiError):
            await links.approve(ctx, created["userCode"])  # type: ignore[arg-type]
        assert links.view(created["userCode"])["client"] == "term"

    @pytest.mark.parametrize("swept", [True, False])
    async def test_a_request_that_expires_during_the_approval_revokes_the_new_token(self, monkeypatch, swept):
        from types import SimpleNamespace

        from qodalis_cli_users.controllers import tokens_controller

        now = [1_000_000]
        audited: list[str] = []

        async def audit(event, *_args, **_kwargs):
            audited.append(event)

        links = LinkService(SimpleNamespace(audit=audit), "/qcli/admin/link", now=lambda: now[0])  # type: ignore[arg-type]
        revoked: list[str] = []

        async def expiring_create(_auth, _ctx, name, _perms, expires_at):
            # The request expires while the token is created (swept already, or not yet)
            now[0] += LINK_EXPIRES_IN_SECONDS * 1000
            if swept:
                links.sweep()
            return "qcli_pat_x", SimpleNamespace(id="t1", name=name, expires_at=expires_at)

        async def revoke(_auth, _ctx, record):
            revoked.append(record.id)

        monkeypatch.setattr(tokens_controller, "create_api_token", expiring_create)
        monkeypatch.setattr(tokens_controller, "revoke_api_token", revoke)
        created = links.create({"client": "term"})
        ctx = SimpleNamespace(api_token=None, session=object(), external=False, user=SimpleNamespace(id="u1", name="root"))
        with pytest.raises(UsersApiError) as exc:
            await links.approve(ctx, created["userCode"])  # type: ignore[arg-type]
        assert exc.value.status_code == 404 and exc.value.message == "link request not found or expired"
        assert revoked == ["t1"]
        assert "link_approved" not in audited
        assert links.poll(created["linkId"]) is None


# =========================================================================
# Routes on a protected server
# =========================================================================


class Srv:
    def __init__(self, client: TestClient) -> None:
        self.c = client
        self._ips = itertools.count(1)

    def login(self, name: str, password: str) -> str:
        res = self.c.post(BASE + "/sessions", json={"username": name, "password": password})
        assert res.status_code == 201, res.text
        return res.json()["token"]

    def create(self, body: Any = None):
        ip = next(self._ips)
        return self.c.post(
            BASE + "/link",
            json={"client": CLIENT} if body is None else body,
            headers={"Origin": ORIGIN, "X-Forwarded-For": f"10.0.{ip // 250}.{ip % 250}"},
        )

    def tokens(self, session: str) -> list[dict[str, Any]]:
        return self.c.get(BASE + "/tokens", headers=_h(session)).json()


@pytest.fixture(scope="module")
def srv():
    result = create_cli_server(CliServerOptions(
        configure=lambda b: b.add_processor(CliEchoCommandProcessor()),
        allowed_origins=[ORIGIN],
    ))
    users = (
        CliUsersBuilder()
        .set_default_admin_password(ROOT_PASS)
        .set_rate_limit(10_000)
        .set_trust_proxy()
        .build()
    )
    result.mount_plugin(users)
    with TestClient(result.app) as client:
        s = Srv(client)
        s.users = users
        s.root = s.login("root", ROOT_PASS)
        res = client.post(BASE + "/users", headers=_h(s.root), json={
            "name": "ann", "email": "ann@x.io", "password": "annpass12", "groups": ["users"],
        })
        assert res.status_code == 201, res.text
        s.ann = s.login("ann", "annpass12")
        yield s


class TestLinkRoutes:
    def test_creates_without_signing_in(self, srv: Srv):
        res = srv.create()
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["expiresIn"] == 600 and body["interval"] == 2
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", body["linkId"])
        assert re.fullmatch(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}", body["userCode"])
        assert body["verificationUrl"] == f"/qcli/admin/link?code={body['userCode']}"
        assert srv.c.get(f"{BASE}/link/{body['linkId']}").json() == {"status": "pending"}

    @pytest.mark.parametrize("body", [
        {},
        {"client": "x" * 101},
        {"client": "a", "permissions": []},
        {"client": "a", "permissions": [1]},
        {"client": "bad\u0007"},
        ["not", "an", "object"],
    ])
    def test_rejects_invalid_bodies(self, srv: Srv, body):
        res = srv.create(body)
        assert res.status_code == 400, res.text
        assert set(res.json()) == {"error"}

    def test_rejects_invalid_json(self, srv: Srv):
        res = srv.c.post(BASE + "/link", content=b"{", headers={"Content-Type": "application/json",
                                                                "X-Forwarded-For": "198.51.100.1"})
        assert res.status_code == 400 and set(res.json()) == {"error"}

    def test_limits_requests_per_client_address(self, srv: Srv):
        def send():
            return srv.c.post(BASE + "/link", json={"client": "spam"}, headers={"X-Forwarded-For": "192.0.2.7"})

        for _ in range(10):
            assert send().status_code == 201
        res = send()
        assert res.status_code == 429
        assert "too many link requests" in res.json()["error"]

    def test_a_request_that_expires_during_the_approval_revokes_the_new_token(self, srv: Srv, monkeypatch):
        from qodalis_cli_users.controllers import tokens_controller

        created = srv.create(body={"client": "expiring"}).json()
        links = srv.users.link_service
        real_create, real_now = tokens_controller.create_api_token, links._now

        async def expiring_create(*args):
            # The request expires (not swept yet) while the token is created
            out = await real_create(*args)
            monkeypatch.setattr(links, "_now", lambda: real_now() + 601_000)
            return out

        monkeypatch.setattr(tokens_controller, "create_api_token", expiring_create)
        res = srv.c.post(f"{BASE}/link/code/{created['userCode']}/approve", headers=_h(srv.ann))
        assert res.status_code == 404 and res.json() == {"error": "link request not found or expired"}
        monkeypatch.setattr(links, "_now", real_now)
        assert not [t for t in srv.tokens(srv.ann) if t["name"] == "link expiring"]
        assert srv.c.get(f"{BASE}/link/{created['linkId']}").status_code == 404

    def test_approval_flow(self, srv: Srv):
        created = srv.create().json()
        link_id, user_code = created["linkId"], created["userCode"]
        assert srv.c.get(f"{BASE}/link/code/{user_code}").status_code == 401

        view = srv.c.get(f"{BASE}/link/code/{user_code.lower().replace('-', '')}", headers=_h(srv.ann))
        assert view.status_code == 200, view.text
        body = view.json()
        assert body["userCode"] == user_code
        assert body["client"] == CLIENT
        assert body["origin"] == ORIGIN
        assert body["permissions"] is None
        assert body["remoteAddress"].startswith("10.0.")
        assert body["expiresAt"] - body["createdAt"] == 600_000

        approved = srv.c.post(f"{BASE}/link/code/{user_code}/approve", headers=_h(srv.ann))
        assert approved.status_code == 200, approved.text
        assert approved.json()["tokenName"] == f"link {CLIENT}"

        poll = srv.c.get(f"{BASE}/link/{link_id}")
        data = poll.json()
        assert data["status"] == "approved" and data["userName"] == "ann"
        assert data["token"].startswith("qcli_pat_")
        assert isinstance(data["expiresAt"], int)
        assert poll.headers["cache-control"] == "no-store"
        gone = srv.c.get(f"{BASE}/link/{link_id}")
        assert gone.status_code == 404 and gone.json() == {"status": "expired"}

        # The token works and is listed with ann's tokens.
        assert srv.c.get(f"{BASE}/commands", headers=_h(data["token"])).status_code == 200
        listed = srv.tokens(srv.ann)
        assert f"link {CLIENT}" in [t["name"] for t in listed]
        assert next(t for t in listed if t["name"] == f"link {CLIENT}")["id"] == approved.json()["tokenId"]

        # Single use.
        assert srv.c.post(f"{BASE}/link/code/{user_code}/approve", headers=_h(srv.ann)).status_code == 404
        entries = srv.c.get(BASE + "/audit", headers=_h(srv.root)).json()
        entry = next(e for e in entries if e["event"] == "link_approved")
        assert entry["username"] == "ann" and entry["actor"] == "ann"
        assert entry["details"] == f"{CLIENT} (link {CLIENT})"
        assert entry["metadata"] == {"client": CLIENT, "origin": ORIGIN, "tokenId": approved.json()["tokenId"]}
        assert any(e["event"] == "token_created" for e in entries)

    def test_second_terminal_gets_numbered_token(self, srv: Srv):
        user_code = srv.create().json()["userCode"]
        approved = srv.c.post(f"{BASE}/link/code/{user_code}/approve", headers=_h(srv.ann))
        assert approved.status_code == 200, approved.text
        assert approved.json()["tokenName"] == f"link {CLIENT} 2"

    def test_denial(self, srv: Srv):
        created = srv.create({"client": "denied term"}).json()
        link_id, user_code = created["linkId"], created["userCode"]
        res = srv.c.post(f"{BASE}/link/code/{user_code}/deny", headers=_h(srv.ann))
        assert res.status_code == 204
        assert srv.c.get(f"{BASE}/link/{link_id}").json() == {"status": "denied"}
        assert srv.c.get(f"{BASE}/link/{link_id}").status_code == 404
        assert srv.c.post(f"{BASE}/link/code/{user_code}/approve", headers=_h(srv.ann)).status_code == 404
        assert srv.c.post(f"{BASE}/link/code/{user_code}/deny", headers=_h(srv.ann)).status_code == 404
        assert "link denied term" not in [t["name"] for t in srv.tokens(srv.ann)]
        entries = srv.c.get(BASE + "/audit", headers=_h(srv.root)).json()
        entry = next(e for e in entries if e["event"] == "link_denied")
        assert entry["metadata"] == {"client": "denied term", "origin": ORIGIN}

    def test_scoped_requests(self, srv: Srv):
        scoped = srv.create({"client": "scoped", "permissions": ["commands:execute"]}).json()
        view = srv.c.get(f"{BASE}/link/code/{scoped['userCode']}", headers=_h(srv.root)).json()
        assert view["permissions"] == ["commands:execute"]
        res = srv.c.post(f"{BASE}/link/code/{scoped['userCode']}/approve", headers=_h(srv.root))
        assert res.status_code == 200, res.text
        token = next(t for t in srv.tokens(srv.root) if t["name"] == "link scoped")
        assert token["permissions"] == ["commands:execute"]

        greedy = srv.create({"client": "greedy", "permissions": ["users:write"]}).json()
        res = srv.c.post(f"{BASE}/link/code/{greedy['userCode']}/approve", headers=_h(srv.ann))
        assert res.status_code == 403
        assert "cannot grant users:write" in res.json()["error"]
        # Still pending: someone allowed to may approve it.
        assert srv.c.get(f"{BASE}/link/{greedy['linkId']}").json() == {"status": "pending"}

    def test_api_tokens_cannot_approve(self, srv: Srv):
        first = srv.create({"client": "via token"}).json()
        assert srv.c.post(f"{BASE}/link/code/{first['userCode']}/approve", headers=_h(srv.ann)).status_code == 200
        token = srv.c.get(f"{BASE}/link/{first['linkId']}").json()["token"]
        second = srv.create({"client": "second"}).json()
        res = srv.c.post(f"{BASE}/link/code/{second['userCode']}/approve", headers=_h(token))
        assert res.status_code == 403
        assert "API tokens cannot create tokens" in res.json()["error"]

    def test_unknown_codes_and_links(self, srv: Srv):
        assert srv.c.get(f"{BASE}/link/code/AAAA-AAAA", headers=_h(srv.ann)).status_code == 404
        res = srv.c.get(f"{BASE}/link/nope")
        assert res.status_code == 404 and res.json() == {"status": "expired"}


class TestExternalAuth:
    def test_approve_answers_501_without_sessions(self):
        from qodalis_cli_users.models import ExternalAuthResult, User

        def adapter(request):
            return ExternalAuthResult(user=User(id="ext-ann", name="ann", email="ann@x.test", groups=[]),
                                      is_admin=True)

        result = create_cli_server(CliServerOptions(allowed_origins=[ORIGIN]))
        users = CliUsersBuilder().set_auth_adapter(adapter).build()
        result.mount_plugin(users)
        with TestClient(result.app) as client:
            created = client.post(BASE + "/link", json={"client": "x"}).json()
            code = created["userCode"]
            assert client.get(f"{BASE}/link/code/{code}").status_code == 200
            res = client.post(f"{BASE}/link/code/{code}/approve", headers={"Origin": ORIGIN})
            assert res.status_code == 501
            # Deny works without a session.
            assert client.post(f"{BASE}/link/code/{code}/deny", headers={"Origin": ORIGIN}).status_code == 204


# =========================================================================
# Approval page option
# =========================================================================


class TestApprovalUrl:
    def test_uses_the_host_page(self):
        result = create_cli_server(CliServerOptions())
        users = (
            CliUsersBuilder()
            .set_default_admin_password(ROOT_PASS)
            .set_link_approval_url("https://app.example.com/cli/link/{code}")
            .build()
        )
        result.mount_plugin(users)
        with TestClient(result.app) as client:
            body = client.post(BASE + "/link", json={"client": "x"}).json()
            assert body["verificationUrl"] == f"https://app.example.com/cli/link/{body['userCode']}"

    @pytest.mark.parametrize("url", ["javascript:alert(1)", "//evil.example.com", "/\\evil", "", "relative/path"])
    def test_rejects_invalid_urls(self, url):
        with pytest.raises(ValueError):
            CliUsersBuilder().set_link_approval_url(url)
