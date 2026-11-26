"""System notifications end to end: the API (``/api/v1/qcli/notifications``), permission filtering and per-user read
state on a protected server, ``notification:changed`` on the events socket (with each socket's own unread count), the
``notifications`` command, the ``notifications_list`` MCP tool, persistence across a restart and the server's own
sources (security warning, restart required, plugins that failed or need configuration).  Port of the Node
``notifications-api.test.ts``."""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any

from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli_server_abstractions import CliNotificationKeys, CliPluginEntryInfo

from .command_helpers import V1, add_users, protect, run_command

NOTIF = f"{V1}/notifications"
MARKER = "test:marker"


def server(tmp_path: Any, **options: Any) -> Any:
    options.setdefault("data_dir", str(tmp_path / "data"))
    return create_cli_server(CliServerOptions(**options))


def note(key: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "kind": "test", "severity": "info", "title": f"About {key}", "message": "Details", **extra}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def code_entry(name: str, fn: Any, package: str | None = None, version: str = "1.0.0") -> Any:
    fn.plugin = CliPluginEntryInfo(id=name, package=package or f"acme-{name}", version=version)
    return fn


# --------------------------------------------------------------------------- open server


def test_lists_reads_reads_all_dismisses_and_clears(tmp_path):
    cli = server(tmp_path)
    client = TestClient(cli.app)
    a = cli.notifications.notify(note("a"))
    cli.notifications.notify(note("b", severity="error", link="/plugins"))

    res = client.get(NOTIF)
    assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
    assert res.json()["unreadCount"] == 2
    assert sorted(n["key"] for n in res.json()["items"]) == ["a", "b"]
    assert [n["key"] for n in client.get(f"{NOTIF}?severity=error").json()["items"]] == ["b"]

    res = client.post(f"{NOTIF}/{a['id']}/read")
    assert res.status_code == 200
    assert res.json()["item"]["id"] == a["id"] and res.json()["item"]["read"] is True and res.json()["unreadCount"] == 1
    assert [n["key"] for n in client.get(f"{NOTIF}?unread=true").json()["items"]] == ["b"]
    assert [n["key"] for n in client.get(f"{NOTIF}?unread=1").json()["items"]] == ["b"]

    assert client.post(f"{NOTIF}/read-all").json() == {"marked": 1, "unreadCount": 0}

    assert client.delete(f"{NOTIF}/{a['id']}").json() == {"dismissed": True, "unreadCount": 0}
    b = cli.notifications.get("b")
    assert client.delete(f"{NOTIF}/{b['id']}?scope=all").json() == {"cleared": True, "unreadCount": 0}
    assert client.get(NOTIF).json() == {"items": [], "unreadCount": 0}
    assert cli.notifications.get("b") is None


def test_the_json_of_a_notification(tmp_path):
    cli = server(tmp_path)
    cli.notifications.notify(note("full", link="/x", requiredPermission="admin:access", data={"n": 1}))
    cli.notifications.notify(note("bare"))
    items = {n["key"]: n for n in TestClient(cli.app).get(NOTIF).json()["items"]}
    assert set(items["full"]) == {
        "id", "key", "kind", "severity", "title", "message", "link", "requiredPermission", "data", "createdAt", "updatedAt", "read",
    }
    # optional fields are left out when unset
    assert set(items["bare"]) == {"id", "key", "kind", "severity", "title", "message", "createdAt", "updatedAt", "read"}
    assert items["bare"]["createdAt"].endswith("Z")


def test_answers_404_for_an_unknown_id_and_400_for_a_bad_filter(tmp_path):
    client = TestClient(server(tmp_path).app)
    assert client.post(f"{NOTIF}/n_nope/read").status_code == 404
    assert client.post(f"{NOTIF}/n_nope/read").json() == {"error": "notification not found"}
    assert client.delete(f"{NOTIF}/n_nope").status_code == 404
    assert client.delete(f"{NOTIF}/n_nope?scope=all").status_code == 404
    assert client.get(f"{NOTIF}?severity=fatal").status_code == 400
    assert client.get(f"{NOTIF}?limit=0").status_code == 400
    assert client.get(f"{NOTIF}?limit=501").status_code == 400
    assert client.get(f"{NOTIF}?limit=abc").status_code == 400
    assert client.get(f"{NOTIF}?limit=1").json()["items"] == []


def test_persists_across_a_restart_same_data_directory(tmp_path):
    data_dir = str(tmp_path / "kept")
    first = server(tmp_path, data_dir=data_dir)
    n = first.notifications.notify(note("kept"))
    assert TestClient(first.app).post(f"{NOTIF}/{n['id']}/read").status_code == 200
    body = TestClient(server(tmp_path, data_dir=data_dir).app).get(NOTIF).json()
    assert body["unreadCount"] == 0
    assert [(i["id"], i["key"], i["read"]) for i in body["items"]] == [(n["id"], "kept", True)]


def test_memory_only_with_notifications_file_false(tmp_path):
    data_dir = str(tmp_path / "mem")
    server(tmp_path, data_dir=data_dir, notifications={"file": False}).notifications.notify(note("gone"))
    assert TestClient(server(tmp_path, data_dir=data_dir).app).get(NOTIF).json()["items"] == []


def test_options_choose_the_file_and_the_limits(tmp_path):
    file = str(tmp_path / "elsewhere" / "notes.json")
    cli = server(tmp_path, notifications={"file": file, "maxItems": 1})
    cli.notifications.notify(note("a"))
    cli.notifications.notify(note("b"))
    assert cli.notifications.path == file
    with open(file, encoding="utf-8") as f:
        assert [i["key"] for i in json.load(f)["items"]] == ["b"]


# --------------------------------------------------------------------------- protected server


def test_filters_by_permission_keeps_the_read_state_per_user_and_clearing_for_everyone_needs_manage(tmp_path):
    cli = server(tmp_path)
    protect(cli)
    with TestClient(cli.app) as client:
        tokens = add_users(client, {
            "admin": ["admin:access"], "plain": ["commands:execute"], "manager": ["notifications:manage"],
        })
        admin_only = cli.notifications.notify(note("admin-only", requiredPermission="admin:access"))
        everyone = cli.notifications.notify(note("everyone"))

        assert client.get(NOTIF).status_code == 401

        def keys(who: str) -> list[str]:
            res = client.get(NOTIF, headers=bearer(tokens[who]))
            assert res.status_code == 200, res.text
            return sorted(n["key"] for n in res.json()["items"])

        assert keys("admin") == ["admin-only", "everyone"]
        assert keys("plain") == ["everyone"]
        assert keys("root") == ["admin-only", "everyone"]

        # a hidden one is not found
        assert client.post(f"{NOTIF}/{admin_only['id']}/read", headers=bearer(tokens["plain"])).status_code == 404
        assert client.delete(f"{NOTIF}/{admin_only['id']}", headers=bearer(tokens["plain"])).status_code == 404

        assert client.post(f"{NOTIF}/{everyone['id']}/read", headers=bearer(tokens["admin"])).status_code == 200
        assert client.get(NOTIF, headers=bearer(tokens["admin"])).json()["unreadCount"] == 1
        assert client.get(NOTIF, headers=bearer(tokens["plain"])).json()["unreadCount"] == 1

        # dismiss: for the caller only
        assert client.delete(f"{NOTIF}/{everyone['id']}", headers=bearer(tokens["plain"])).status_code == 200
        assert keys("plain") == []
        assert keys("admin") == ["admin-only", "everyone"]

        # for everyone: notifications:manage
        refused = client.delete(f"{NOTIF}/{everyone['id']}?scope=all", headers=bearer(tokens["admin"]))
        assert refused.status_code == 403
        assert refused.json() == {"error": "forbidden: missing permission notifications:manage"}
        assert client.delete(f"{NOTIF}/{everyone['id']}?scope=all", headers=bearer(tokens["manager"])).status_code == 200
        assert keys("admin") == ["admin-only"]


def test_lists_the_new_permissions_in_the_catalog(tmp_path):
    cli = server(tmp_path)
    for name in ("notifications:manage", "updates:read"):
        entry = cli.builder.permissions.find(name)
        assert (entry.group, entry.surface) == ("Server", "cli")
    names = [p.name for p in cli.builder.permissions.all()]
    assert names[names.index("server:restart") + 1: names.index("server:restart") + 3] == ["updates:read", "notifications:manage"]


# --------------------------------------------------------------------------- events socket


def notification_events(client: TestClient, cli: Any, ws: Any) -> list[dict[str, Any]]:
    """The ``notification:changed`` messages received so far: the events published from synchronous code are
    delivered by tasks of the app's loop, so a marker published to everyone after they ran ends the read."""
    marker = f"{MARKER}-{next(_markers)}"
    client.portal.call(asyncio.sleep, 0.02)
    client.portal.call(cli.event_bus.publish, marker, {}, None)
    messages: list[dict[str, Any]] = []
    while True:
        message = json.loads(ws.receive_text())
        if message["type"] == marker:
            return messages
        if message["type"] == "notification:changed":
            messages.append(message)


_markers = itertools.count(1)


def test_open_server_the_event_carries_the_action_the_item_and_the_unread_count(tmp_path):
    cli = server(tmp_path)
    with TestClient(cli.app) as client, client.websocket_connect("/ws/v1/qcli/events") as ws:
        assert json.loads(ws.receive_text())["type"] == "connected"
        n = client.portal.call(cli.notifications.notify, note("ws", severity="warning"))
        [created] = notification_events(client, cli, ws)
        assert {k: created[k] for k in ("action", "id", "key", "unreadCount")} == {
            "action": "created", "id": n["id"], "key": "ws", "unreadCount": 1,
        }
        assert created["timestamp"].endswith("Z")
        assert (created["item"]["title"], created["item"]["severity"], created["item"]["read"]) == ("About ws", "warning", False)
        assert "readBy" not in created["item"]

        assert client.post(f"{NOTIF}/{n['id']}/read").status_code == 200
        [read] = notification_events(client, cli, ws)
        assert (read["action"], read["unreadCount"], read["item"]["read"]) == ("read", 0, True)

        client.portal.call(cli.notifications.resolve, "ws")
        [resolved] = notification_events(client, cli, ws)
        assert (resolved["action"], resolved["key"], resolved["unreadCount"]) == ("resolved", "ws", 0)
        assert "item" not in resolved


def test_protected_server_each_socket_gets_its_own_unread_count_and_a_user_without_the_permission_nothing(tmp_path):
    cli = server(tmp_path)
    protect(cli)
    with TestClient(cli.app) as client:
        tokens = add_users(client, {"admin": ["admin:access"], "plain": ["commands:execute"]})

        def ticket(token: str) -> str:
            res = client.post(f"{V1}/sessions/ws-ticket", headers=bearer(token))
            assert res.status_code == 201, res.text
            return res.json()["ticket"]

        with client.websocket_connect(f"/ws/v1/qcli/events?ticket={ticket(tokens['admin'])}") as admin_ws, \
                client.websocket_connect(f"/ws/v1/qcli/events?ticket={ticket(tokens['plain'])}") as plain_ws:
            admin_ws.receive_text()
            plain_ws.receive_text()
            client.portal.call(cli.notifications.notify, note("public"))
            [a1] = notification_events(client, cli, admin_ws)
            [p1] = notification_events(client, cli, plain_ws)
            assert (a1["key"], a1["unreadCount"]) == ("public", 1)
            assert (p1["key"], p1["unreadCount"]) == ("public", 1)

            client.portal.call(cli.notifications.notify, note("secret", requiredPermission="admin:access"))
            [a2] = notification_events(client, cli, admin_ws)
            assert (a2["action"], a2["key"], a2["unreadCount"]) == ("created", "secret", 2)

            # the admin reads everything: only the admin's sockets hear of it
            assert client.post(f"{NOTIF}/read-all", headers=bearer(tokens["admin"])).status_code == 200
            [a3] = notification_events(client, cli, admin_ws)
            assert (a3["action"], a3["unreadCount"]) == ("read-all", 0)
            assert notification_events(client, cli, plain_ws) == []


def test_in_process_subscribers_get_the_event_without_personal_fields(tmp_path):
    cli = server(tmp_path)
    events: list[Any] = []
    cli.event_bus.subscribe("notification:changed", events.append)
    cli.notifications.notify(note("plain"))
    assert [e.data["action"] for e in events] == ["created"]
    assert "unreadCount" not in events[0].data and "read" not in events[0].data["item"]


# --------------------------------------------------------------------------- command and MCP


def test_the_command_lists_reads_and_clears(tmp_path):
    cli = server(tmp_path)
    with TestClient(cli.app) as client:
        n = cli.notifications.notify(note("cmd", severity="error", title="Something broke"))
        out = run_command(client, cli, "notifications list").text
        assert "1 notification(s), 1 unread" in out
        assert "Something broke" in out and n["id"] in out
        assert out.splitlines()[1].startswith("* x ")
        assert "      Details" in out
        assert "Something broke" in run_command(client, cli, "notifications").text

        assert run_command(client, cli, f"notifications read {n['id'][:6]}").text == "Read: Something broke"
        assert run_command(client, cli, "notifications list --unread").text == "No unread notifications."
        assert run_command(client, cli, "notifications read --all").text == "Marked 0 notification(s) read."
        assert run_command(client, cli, "notifications read n_missing").text == "Error: notification not found: n_missing"

        # by key; an ambiguous prefix
        assert run_command(client, cli, "notifications read cmd").text == "Read: Something broke"
        cli.notifications.notify(note("other"))
        assert "matches 2 notifications; use the full id" in run_command(client, cli, "notifications read n_").text

        assert run_command(client, cli, f"notifications clear {n['id']}").text == "Dismissed: Something broke"
        cli.notifications.resolve("other")
        assert run_command(client, cli, "notifications list").text == "No notifications."


def test_clear_all_needs_notifications_manage_on_a_protected_server(tmp_path):
    cli = server(tmp_path)
    protect(cli)
    with TestClient(cli.app) as client:
        tokens = add_users(client, {
            "plain": ["commands:execute"], "manager": ["commands:execute", "notifications:manage"],
        })
        n = cli.notifications.notify(note("shared"))
        refused = run_command(client, cli, f"notifications clear {n['id']} --all", tokens["plain"])
        assert refused.text == "Error: forbidden: missing permission notifications:manage"
        assert run_command(client, cli, f"notifications clear {n['id']} --all", tokens["manager"]).text == (
            "Cleared for everyone: About shared"
        )
        assert cli.notifications.get("shared") is None


def test_the_command_is_listed_in_its_module(tmp_path):
    cli = server(tmp_path)
    processor = cli.registry.find_processor("notifications")
    assert (processor.module, processor.icon, processor.required_permissions) == ("notifications", "🔔", None)
    assert [p.command for p in processor.processors] == ["list", "read", "clear"]


def test_the_mcp_tool_lists_the_callers_notifications(tmp_path):
    cli = server(tmp_path, mcp=True)
    cli.notifications.notify(note("mcp", title="From MCP"))
    with TestClient(cli.app) as client:
        res = client.post("/api/qcli/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "notifications_list", "arguments": {"unread": True}},
        })
        assert res.status_code == 200, res.text
        result = res.json()["result"]
        assert result["isError"] is False
        assert result["structuredContent"]["unreadCount"] == 1
        assert [(i["key"], i["title"]) for i in result["structuredContent"]["items"]] == [("mcp", "From MCP")]
        listed = client.post("/api/qcli/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tool = next(t for t in listed.json()["result"]["tools"] if t["name"] == "notifications_list")
        assert tool["annotations"] == {"readOnlyHint": True}


def test_no_mcp_tool_without_mcp(tmp_path):
    cli = server(tmp_path)
    assert not any(getattr(p, "name", None) == "notifications" for p in cli.mcp.providers)


# --------------------------------------------------------------------------- the server's own sources


def test_warns_while_allow_anonymous_admin_is_on_and_resolves_it_once_off(tmp_path):
    data_dir = str(tmp_path / "sec")
    opened = server(tmp_path, data_dir=data_dir, allow_anonymous_admin=True)
    warning = opened.notifications.get(CliNotificationKeys.ANONYMOUS_ADMIN)
    assert (warning["kind"], warning["severity"], warning["requiredPermission"], warning["link"]) == (
        "security", "warning", "admin:access", "/config",
    )
    assert warning["title"] == "Anyone can administer this server"
    closed = server(tmp_path, data_dir=data_dir)
    assert closed.notifications.get(CliNotificationKeys.ANONYMOUS_ADMIN) is None


def test_says_when_a_restart_is_required_and_resolves_it_when_nothing_waits(tmp_path):
    cli = server(tmp_path)
    cli.restart.mark_required("config:cors.cors.mode", "config", "CORS: Mode changed")
    n = cli.notifications.get(CliNotificationKeys.RESTART_REQUIRED)
    assert (n["kind"], n["severity"], n["message"], n["data"], n["link"]) == (
        "restart", "warning", "CORS: Mode changed", {"pending": ["config:cors.cors.mode"]}, "/",
    )
    assert n["title"] == "Restart required: 1 change waits for a restart"
    cli.restart.mark_required("x", "test", "Another")
    n = cli.notifications.get(CliNotificationKeys.RESTART_REQUIRED)
    assert n["title"] == "Restart required: 2 changes wait for a restart"
    assert n["message"] == "CORS: Mode changed\nAnother"
    cli.restart.clear_required("config:cors.cors.mode")
    cli.restart.clear_required("x")
    assert cli.notifications.get(CliNotificationKeys.RESTART_REQUIRED) is None


def test_a_restarted_server_nothing_pending_resolves_the_restart_notification_it_kept(tmp_path):
    data_dir = str(tmp_path / "restart")
    first = server(tmp_path, data_dir=data_dir)
    first.restart.mark_required("x", "test", "Pending")
    assert first.notifications.get(CliNotificationKeys.RESTART_REQUIRED) is not None
    assert server(tmp_path, data_dir=data_dir).notifications.get(CliNotificationKeys.RESTART_REQUIRED) is None


def test_reports_plugins_that_failed_or_need_configuration_and_resolves_them_once_they_load(tmp_path):
    data_dir = str(tmp_path / "plugins")

    def broken(context: Any) -> Any:
        raise RuntimeError("cannot connect")

    def unconfigured(context: Any) -> Any:
        return context.needs_configuration(["settings.apiKey"])

    first = server(tmp_path, data_dir=data_dir, configure=lambda b: (
        b.add_plugin(code_entry("broken", broken, "acme-broken")),
        b.add_plugin(code_entry("needy", unconfigured, "acme-needy", "2.0.0")),
    ))
    with TestClient(first.app):
        failed = first.notifications.get("plugin.failed:broken")
        assert (failed["kind"], failed["severity"], failed["title"], failed["message"], failed["link"]) == (
            "plugin", "error", "Plugin broken failed to load", "acme-broken@1.0.0: cannot connect", "/plugins/acme-broken",
        )
        assert failed["data"] == {
            "plugin": "broken", "package": "acme-broken", "version": "1.0.0", "source": "code", "error": "cannot connect",
        }
        needs = first.notifications.get("plugin.needs-configuration:needy")
        assert (needs["severity"], needs["message"], needs["data"]["missing"], needs["link"]) == (
            "warning", "Set settings.apiKey, then restart the server.", ["settings.apiKey"], "/config?provider=needy",
        )

    # next start: both load
    second = server(tmp_path, data_dir=data_dir, configure=lambda b: (
        b.add_plugin(code_entry("broken", lambda c: None, "acme-broken")),
        b.add_plugin(code_entry("needy", lambda c: None, "acme-needy", "2.0.0")),
    ))
    assert second.notifications.get("plugin.failed:broken") is not None  # kept until this start loaded them
    with TestClient(second.app):
        assert second.notifications.get("plugin.failed:broken") is None
        assert second.notifications.get("plugin.needs-configuration:needy") is None


def test_a_start_without_plugins_resolves_the_plugin_notifications_it_kept(tmp_path):
    data_dir = str(tmp_path / "gone")

    def broken(context: Any) -> Any:
        raise RuntimeError("boom")

    first = server(tmp_path, data_dir=data_dir, configure=lambda b: b.add_plugin(code_entry("broken", broken)))
    with TestClient(first.app):
        assert first.notifications.get("plugin.failed:broken") is not None
    second = server(tmp_path, data_dir=data_dir)
    with TestClient(second.app):
        assert second.notifications.get("plugin.failed:broken") is None


def test_plugins_publish_through_their_context_and_the_builder(tmp_path):
    def talker(context: Any) -> Any:
        context.notifications.notify({
            "key": "talker:hello", "kind": "talker", "severity": "info", "title": "Hello from a plugin", "message": "",
        })

    cli = server(tmp_path, configure=lambda b: b.add_plugin(code_entry("talker", talker)))
    cli.builder.notifications.notify(note("from-builder"))
    with TestClient(cli.app):
        assert cli.notifications.get("talker:hello")["title"] == "Hello from a plugin"
        assert cli.notifications.get("from-builder") is not None
