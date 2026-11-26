"""The notifications service: dedupe by key, resolve and resolve_missing, permission filtering, per-user read and
dismissed state, order and filters, persistence, retention, invalid input and the ``notification:changed`` events."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from qodalis_cli import CliEventBus
from qodalis_cli.notifications import CliNotificationService, CliNotificationViewer
from qodalis_cli_server_abstractions import CLI_NOTIFICATION_CHANGED_EVENT


def viewer(user_id: str, permissions: tuple[str, ...] = ()) -> CliNotificationViewer:
    return CliNotificationViewer(user_id, lambda p: "*" in permissions or p in permissions)


ALICE = viewer("alice", ("admin:access",))
BOB = viewer("bob")


def note(key: str, **extra):
    return {"key": key, "kind": "test", "severity": "info", "title": f"Title {key}", "message": "Details", **extra}


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class Clock:
    def __init__(self, start: str) -> None:
        self.now = at(start)

    def __call__(self) -> datetime:
        return self.now


async def test_dedupes_by_key_identical_content_changes_nothing_changed_content_is_unread_again():
    bus = CliEventBus()
    events = []
    bus.subscribe(CLI_NOTIFICATION_CHANGED_EVENT, events.append)
    clock = Clock("2026-11-26T10:00:00Z")
    s = CliNotificationService(event_bus=lambda: bus, now=clock)
    first = s.notify(note("a"))
    assert first["key"] == "a" and first["kind"] == "test" and first["title"] == "Title a" and first["read"] is False
    assert re.fullmatch(r"n_[0-9a-f]{16}", first["id"])

    s.mark_read(ALICE, first["id"])
    # identical: no update, still read, no event
    s.notify(note("a"))
    assert len(s.list(ALICE)["items"]) == 1
    assert s.list(ALICE)["items"][0]["read"] is True

    clock.now = at("2026-11-26T11:00:00Z")
    updated = s.notify(note("a", severity="warning", title="Changed"))
    assert updated["id"] == first["id"]
    assert updated["createdAt"] == "2026-11-26T10:00:00.000Z"
    assert updated["updatedAt"] == "2026-11-26T11:00:00.000Z"
    listed = s.list(ALICE)
    assert listed["unreadCount"] == 1
    assert [(n["title"], n["severity"], n["read"]) for n in listed["items"]] == [("Changed", "warning", False)]
    await _settle()
    assert [e.data["action"] for e in events] == ["created", "read", "updated"]


async def _settle() -> None:
    import asyncio

    for _ in range(5):
        await asyncio.sleep(0)


def test_resolve_removes_a_key_and_resolve_missing_keeps_only_the_listed_keys_of_a_prefix():
    s = CliNotificationService()
    s.notify(note("update:a"))
    s.notify(note("update:b"))
    s.notify(note("other"))
    assert s.resolve("update:a") is True
    assert s.resolve("update:a") is False
    s.notify(note("update:c"))
    assert s.resolve_missing("update:", ["update:c"]) == 1
    assert sorted(n["key"] for n in s.list(ALICE)["items"]) == ["other", "update:c"]
    assert s.get("update:b") is None


def test_filters_by_required_permission_and_hidden_ones_cannot_be_read_dismissed_or_cleared():
    s = CliNotificationService()
    s.notify(note("public"))
    s.notify(note("admin", requiredPermission="admin:access"))
    assert sorted(n["key"] for n in s.list(ALICE)["items"]) == ["admin", "public"]
    assert [n["key"] for n in s.list(BOB)["items"]] == ["public"]
    assert s.unread_count(BOB) == 1
    hidden = s.get("admin")
    assert s.mark_read(BOB, hidden["id"]) is None
    assert s.dismiss(BOB, hidden["id"]) is False
    assert s.clear(BOB, hidden["id"]) is False
    # a wildcard permission sees it
    assert "admin" in [n["key"] for n in s.list(viewer("root", ("*",)))["items"]]


def test_keeps_the_read_state_per_user_read_all_and_dismiss_affect_only_that_user():
    s = CliNotificationService()
    a = s.notify(note("a"))
    s.notify(note("b"))
    s.mark_read(ALICE, a["id"])
    assert s.unread_count(ALICE) == 1
    assert s.unread_count(BOB) == 2
    assert [n["key"] for n in s.list(ALICE, unread=True)["items"]] == ["b"]
    assert s.mark_all_read(BOB) == 2
    assert s.unread_count(BOB) == 0
    assert s.unread_count(ALICE) == 1

    assert s.dismiss(ALICE, a["id"]) is True
    assert [n["key"] for n in s.list(ALICE)["items"]] == ["b"]
    assert len(s.list(BOB)["items"]) == 2
    # clear removes it for everyone
    assert s.clear(BOB, a["id"]) is True
    assert [n["key"] for n in s.list(BOB)["items"]] == ["b"]


def test_a_dismissed_notification_comes_back_when_its_content_changes():
    s = CliNotificationService()
    a = s.notify(note("a"))
    s.dismiss(ALICE, a["id"])
    assert s.list(ALICE)["items"] == []
    s.notify(note("a", message="New details"))
    [item] = s.list(ALICE)["items"]
    assert (item["key"], item["read"], item["message"]) == ("a", False, "New details")


def test_lists_newest_first_and_filters_by_severity_and_limit():
    clock = Clock("2026-11-26T10:00:00Z")

    def tick():
        clock.now += timedelta(seconds=1)
        return clock.now

    s = CliNotificationService(now=tick)
    s.notify(note("a"))
    s.notify(note("b", severity="error"))
    s.notify(note("c"))
    assert [n["key"] for n in s.list(ALICE)["items"]] == ["c", "b", "a"]
    assert [n["key"] for n in s.list(ALICE, severity="error")["items"]] == ["b"]
    assert [n["key"] for n in s.list(ALICE, limit=2)["items"]] == ["c", "b"]
    assert s.list(ALICE, limit=2)["unreadCount"] == 3


def test_persists_across_a_restart_read_and_dismissed_state_included(tmp_path):
    file = str(tmp_path / "data" / "notifications.json")
    s1 = CliNotificationService(file=file)
    a = s1.notify(note("a", requiredPermission="admin:access", link="/plugins", data={"n": 1}))
    b = s1.notify(note("b"))
    s1.mark_read(ALICE, a["id"])
    s1.dismiss(BOB, b["id"])
    with open(file, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["version"] == 1 and len(saved["items"]) == 2
    assert next(i for i in saved["items"] if i["key"] == "a")["readBy"] == ["alice"]

    s2 = CliNotificationService(file=file)
    item = next(n for n in s2.list(ALICE)["items"] if n["key"] == "a")
    assert (item["id"], item["read"], item["link"], item["data"]) == (a["id"], True, "/plugins", {"n": 1})
    assert s2.list(BOB)["items"] == []
    assert s2.resolve("a") is True
    assert CliNotificationService(file=file).get("a") is None
    # no temporary file left behind
    assert sorted(p.name for p in (tmp_path / "data").iterdir()) == ["notifications.json"]


def test_caps_the_list_and_drops_notifications_30_days_after_their_last_change():
    clock = Clock("2026-11-01T00:00:00Z")
    s = CliNotificationService(max_items=3, now=clock)
    for k in ("a", "b", "c", "d"):
        clock.now += timedelta(minutes=1)
        s.notify(note(k))
    assert [n["key"] for n in s.list(ALICE)["items"]] == ["d", "c", "b"]
    clock.now = at("2026-12-01T00:02:30Z")
    assert [n["key"] for n in s.list(ALICE)["items"]] == ["d", "c"]
    clock.now = at("2026-12-02T00:00:00Z")
    assert s.list(ALICE)["items"] == []


def test_options_are_checked():
    with pytest.raises(ValueError, match="max_items"):
        CliNotificationService(max_items=0)
    with pytest.raises(ValueError, match="max_age_days"):
        CliNotificationService(max_age_days=0)


def test_ignores_a_damaged_file_and_damaged_entries(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json")
    assert CliNotificationService(file=str(bad)).list(ALICE)["items"] == []
    partial = tmp_path / "partial.json"
    now = datetime.now(timezone.utc).isoformat()
    partial.write_text(json.dumps({"version": 1, "items": [
        {"id": "n_1", "key": "ok", "kind": "k", "severity": "info", "title": "T", "message": "", "createdAt": now, "updatedAt": now},
        {"key": "broken"},
    ]}))
    assert [n["key"] for n in CliNotificationService(file=str(partial)).list(ALICE)["items"]] == ["ok"]


def test_refuses_invalid_input():
    s = CliNotificationService()
    with pytest.raises(ValueError, match="key"):
        s.notify(note("bad key with spaces"))
    with pytest.raises(ValueError, match="kind"):
        s.notify(note("k", kind="Bad Kind"))
    with pytest.raises(ValueError, match="severity"):
        s.notify(note("k", severity="fatal"))
    with pytest.raises(ValueError, match="title"):
        s.notify(note("k", title=""))
    with pytest.raises(ValueError, match="message"):
        s.notify(note("k", message="x" * 2001))
    with pytest.raises(ValueError, match="link"):
        s.notify(note("k", link="javascript:alert(1)"))
    with pytest.raises(ValueError, match="link"):
        s.notify(note("k", link="//evil.test"))
    with pytest.raises(ValueError, match="requiredPermission"):
        s.notify(note("k", requiredPermission="admin"))
    with pytest.raises(ValueError, match="8 KB"):
        s.notify(note("k", data={"big": "x" * 9000}))
    assert s.notify(note("k", link="https://example.test/x"))["link"] == "https://example.test/x"
    # snake_case and trimmed texts
    n = s.notify(note("k2", title="  Spaced  ", required_permission="admin:access"))
    assert (n["title"], n["requiredPermission"]) == ("Spaced", "admin:access")
    assert "link" not in s.notify(note("k3"))


async def test_publishes_notification_changed_with_the_item_to_the_permission_or_the_user():
    bus = CliEventBus()
    events = []
    bus.subscribe(CLI_NOTIFICATION_CHANGED_EVENT, events.append)
    s = CliNotificationService(event_bus=lambda: bus)
    n = s.notify(note("a", requiredPermission="admin:access"))
    s.mark_read(ALICE, n["id"])
    s.resolve("a")
    s.notify(note("b"))
    s.mark_all_read(BOB)
    await _settle()
    assert [(e.data["action"], e.audience.permission, e.audience.user_id) for e in events] == [
        ("created", "admin:access", None),
        ("read", None, "alice"),
        ("resolved", "admin:access", None),
        ("created", None, None),
        ("read-all", None, "bob"),
    ]
    assert events[0].data["item"]["id"] == n["id"] and events[0].data["item"]["title"] == "Title a"
    assert "readBy" not in events[0].data["item"]
    assert "item" not in events[2].data and events[2].data["key"] == "a"
    assert "id" not in events[4].data


def test_publishes_from_synchronous_code_without_an_event_loop():
    bus = CliEventBus()
    events = []
    bus.subscribe(CLI_NOTIFICATION_CHANGED_EVENT, events.append)
    s = CliNotificationService(event_bus=lambda: bus)
    s.notify(note("sync"))
    assert [e.data["action"] for e in events] == ["created"]
