"""Settings of dashboard plugins from the marketplace (admin plugin): the ``dashboard-plugins`` config provider with a
section per installed plugin with settings (entries and defaults from the version's ``config.entries``), install-time
values checked before anything is downloaded, values kept or dropped on update, deleted on remove, restored after a
restart from installed.json (offline), invalid or secret settings from the marketplace refused, GET
/installed/{name}/config, ``config set`` through the existing command, ``config:changed``, the store resolution,
permissions, and ``admin dashboard install|update --config`` and ``admin dashboard info``.  No network."""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Optional
from urllib.parse import quote

import pytest

from qodalis_cli import CliServerConfigOptions
from qodalis_cli_admin import (
    DASHBOARD_PLUGIN_CONFIG_MAX_ENTRIES,
    parse_dashboard_plugin_config,
    validate_dashboard_plugin_config_values,
)
from qodalis_cli_server_abstractions.config import InMemoryConfigStore, JsonFileConfigStore

from .command_helpers import V1
from .dashboard_marketplace_helpers import BASE, MARKET, TICKETS, FakeNet, Host, start  # noqa: F401 - start is a fixture

CONFIG = f"{V1}/config"
SECTION = f"{CONFIG}/dashboard-plugins/tickets"

#: The settings of the tickets page, version 1.
SETTINGS_V1 = {
    "entries": [
        {
            "key": "refreshSeconds", "label": "Refresh every", "description": "Seconds between two refreshes",
            "type": "number", "default": 30, "validation": {"min": 5, "max": 3600},
        },
        {"key": "columns", "type": "string[]", "default": ["name", "status"]},
        {"key": "mode", "type": "enum", "default": "compact", "validation": {"options": ["compact", "full"]}},
    ],
}

#: Version 2: refreshSeconds at most 30, mode gone, columns unchanged, theme new.
SETTINGS_V2 = {
    "entries": [
        {"key": "refreshSeconds", "label": "Refresh every", "type": "number", "default": 15, "validation": {"min": 5, "max": 30}},
        {"key": "columns", "type": "string[]", "default": ["name"]},
        {"key": "theme", "type": "string", "default": "light", "validation": {"options": ["light", "dark"]}},
    ],
}


def config_events(h: Host) -> list[Any]:
    events: list[Any] = []
    h.result.event_bus.subscribe("config:*", events.append)
    return events


def publish_tickets(net: FakeNet, versions: Optional[list[dict[str, Any]]] = None) -> None:
    net.publish(TICKETS, versions or [{"version": "1.0.0", "config": SETTINGS_V1}], title="Tickets", icon="🎫")


def cdn_calls(net: FakeNet) -> list[str]:
    return [c for c in net.calls if not c.startswith(MARKET)]


def saved_plugins(h: Host) -> list[dict[str, Any]]:
    with open(os.path.join(h.dir, "installed.json"), encoding="utf-8") as f:
        return json.load(f)["plugins"]


def values_of(h: Host, user: Optional[str] = None) -> Any:
    return h.get(f"{BASE}/installed/{TICKETS}/config", user).json()


def sections(h: Host, user: Optional[str] = None) -> list[dict[str, Any]]:
    return h.get(f"{CONFIG}/dashboard-plugins", user).json()["sections"]


def parsed(raw: Any) -> Any:
    config = parse_dashboard_plugin_config(raw)
    return config.to_dict() if config is not None else None


def audit_of(h: Host, event: str) -> Any:
    return next((a for a in h.audit if a.event == event), None)


# --------------------------------------------------------------------------- the marketplace record


def test_parses_a_config_known_fields_kept_defaults_filled_with_none_no_entries_is_no_settings():
    assert parsed({
        "entries": [
            {"key": "a", "type": "string", "extra": 1},
            {
                "key": "b.c-d_e", "label": "B", "description": "The b", "type": "duration", "default": "30s",
                "validation": {"min": 1000, "unknown": True},
            },
            {"key": "u", "type": "url", "default": "https://x.io"},
            {"key": "j", "type": "json", "default": {"x": [1]}},
            {"key": "f", "type": "boolean", "default": False},
        ],
        "other": True,
    }) == {
        "entries": [
            {"key": "a", "type": "string", "default": None},
            {"key": "b.c-d_e", "label": "B", "description": "The b", "type": "duration", "default": "30s", "validation": {"min": 1000}},
            {"key": "u", "type": "url", "default": "https://x.io"},
            {"key": "j", "type": "json", "default": {"x": [1]}},
            {"key": "f", "type": "boolean", "default": False},
        ],
    }
    assert parse_dashboard_plugin_config(None) is None
    assert parse_dashboard_plugin_config({"entries": []}) is None
    assert parse_dashboard_plugin_config({}) is None


def _entry(**e: Any) -> dict[str, Any]:
    return {"entries": [{"key": "k", "type": "string", **e}]}


@pytest.mark.parametrize("config, reason", [
    (_entry(type="secret"), "k: secret settings are not allowed (a dashboard plugin runs in the browser)"),
    ("text", "config must be an object"),
    ([], "config must be an object"),
    ({"entries": "x"}, "config.entries must be a list"),
    ({"entries": [{"key": f"k{i}", "type": "string"} for i in range(DASHBOARD_PLUGIN_CONFIG_MAX_ENTRIES + 1)]}, "config has more than 50 entries"),
    ({"entries": [{"key": "k", "type": "string", "default": "x" * (33 * 1024)}]}, "config is larger than 32 KB"),
    ({"entries": ["k"]}, "every config entry must be an object"),
    ({"entries": [{"key": "1abc", "type": "string"}]}, "invalid config key: 1abc"),
    ({"entries": [{"key": "a b", "type": "string"}]}, "invalid config key: a b"),
    ({"entries": [{"key": "x" * 65, "type": "string"}]}, f"invalid config key: {'x' * 65}"),
    ({"entries": [{"key": "k", "type": "string"}, {"key": "k", "type": "number"}]}, "duplicate config key: k"),
    (_entry(type="color"), "k: unknown type color"),
    (_entry(label=3), "k: label must be a string"),
    (_entry(validation="x"), "k: validation must be an object"),
    (_entry(validation={"min": "1"}), "k: validation.min must be a number"),
    (_entry(validation={"min": 5, "max": 1}), "k: validation.min is above validation.max"),
    (_entry(validation={"pattern": "(["}), "k: validation.pattern must be a regular expression"),
    (_entry(validation={"options": [1]}), "k: validation.options must be a list of strings"),
    (_entry(validation={"required": "yes"}), "k: validation.required must be true or false"),
    (_entry(type="enum", default="a"), "k: an enum needs validation.options"),
    (_entry(type="enum", default="c", validation={"options": ["a", "b"]}), "k: the default must be one of: a, b"),
    (_entry(type="number", default=1, validation={"min": 5}), "k: the default must be at least 5"),
    (_entry(type="number", default="5"), "k: the default must be a number"),
    (_entry(validation={"required": True}), "k: the default is required"),
    (_entry(type="url", default="not a url"), "k: the default must be an absolute URL"),
])
def test_refuses_a_secret_entry_and_every_malformed_config_with_the_reason(config: Any, reason: str):
    with pytest.raises(ValueError, match=re.escape(reason)):
        parse_dashboard_plugin_config(config)


def test_checks_values_against_the_settings_unknown_keys_types_and_rules_none_keeps_the_default():
    config = parse_dashboard_plugin_config(SETTINGS_V1)
    assert validate_dashboard_plugin_config_values(config, {"refreshSeconds": 60, "columns": ["a"], "mode": "full"}) == {}
    assert validate_dashboard_plugin_config_values(config, {"refreshSeconds": None}) == {}
    assert validate_dashboard_plugin_config_values(config, {"refreshSeconds": 1, "columns": "a", "mode": "huge", "nope": 1}) == {
        "refreshSeconds": "must be at least 5",
        "columns": "must be a list of strings",
        "mode": "must be one of: compact, full",
        "nope": "unknown setting",
    }
    assert validate_dashboard_plugin_config_values(None, {"a": 1}) == {"a": "unknown setting"}


# --------------------------------------------------------------------------- install


def test_install_creates_the_plugins_section_with_the_versions_entries_and_their_defaults(start):
    net = FakeNet()
    publish_tickets(net)
    h = start(net=net)
    res = h.post({"name": TICKETS})
    assert res.status_code == 201
    body = res.json()
    assert (body["name"], body["id"], body["hasConfig"]) == (TICKETS, "tickets", True)
    assert "warnings" not in body

    provider = h.get(f"{CONFIG}/dashboard-plugins")
    assert provider.status_code == 200
    view = provider.json()
    assert (view["id"], view["title"], view["writable"], view["storage"]["kind"], view["store"]["kind"]) == (
        "dashboard-plugins", "Dashboard plugins", True, "store", "memory",
    )
    assert view["sections"] == [
        {
            "id": "tickets",
            "title": "Tickets",
            "description": f"{TICKETS}@1.0.0",
            "mutable": True,
            "version": "0",
            "allowCustomKeys": False,
            "entries": [
                {
                    "key": "refreshSeconds", "label": "Refresh every", "description": "Seconds between two refreshes",
                    "type": "number", "value": 30, "default": 30, "source": "default", "origin": "default", "mutable": True,
                    "requiresRestart": False, "sensitive": False, "validation": {"min": 5, "max": 3600},
                },
                {
                    "key": "columns", "label": "columns", "description": "", "type": "string[]",
                    "value": ["name", "status"], "default": ["name", "status"], "source": "default", "origin": "default", "mutable": True,
                    "requiresRestart": False, "sensitive": False,
                },
                {
                    "key": "mode", "label": "mode", "description": "", "type": "enum", "value": "compact",
                    "default": "compact", "source": "default", "origin": "default", "mutable": True, "requiresRestart": False,
                    "sensitive": False, "validation": {"options": ["compact", "full"]},
                },
            ],
        },
    ]
    # the entries are kept with the plugin, for restarts
    assert saved_plugins(h)[0]["config"] == parsed(SETTINGS_V1)
    # the values the dashboard passes to the page
    values = h.get(f"{BASE}/installed/{TICKETS}/config")
    assert values.status_code == 200
    assert values.headers["cache-control"] == "no-store"
    assert values.json() == {"values": {"refreshSeconds": 30, "columns": ["name", "status"], "mode": "compact"}, "version": "0"}
    # the name encoded works too
    assert h.get(f"{BASE}/installed/{quote(TICKETS, safe='')}/config").json()["version"] == "0"


def test_a_plugin_without_settings_gets_no_section_and_its_config_is_empty(start):
    net = FakeNet()
    publish_tickets(net, [{"version": "1.0.0"}])
    h = start(net=net)
    assert h.post({"name": TICKETS}).json()["hasConfig"] is False
    assert sections(h) == []
    assert values_of(h) == {"values": {}, "version": None}
    assert "config" not in saved_plugins(h)[0]
    # the settings were looked up in the version record: the details had none
    assert f"{MARKET}/api/v1/qcli/marketplace/plugins/{quote(TICKETS, safe='')}/versions/1.0.0?kind=dashboard" in net.calls


def test_get_installed_config_404_not_installed_400_invalid_name_401_signed_out(start):
    h = start()
    assert h.get(f"{BASE}/installed/{TICKETS}/config").status_code == 404
    assert h.get(f"{BASE}/installed/..%2F..%2Fetc/config").status_code == 400
    assert h.client.get(f"{BASE}/installed/{TICKETS}/config").status_code == 401


def test_reads_the_settings_from_the_version_record_when_the_details_leave_them_out(start):
    net = FakeNet()
    net.config_only_in_version_record = True
    publish_tickets(net)
    h = start(net=net)
    assert h.post({"name": TICKETS}).json()["hasConfig"] is True
    assert values_of(h)["values"]["mode"] == "compact"


def test_applies_valid_values_given_with_the_install_stored_audited_config_changed_published(start):
    net = FakeNet()
    publish_tickets(net)
    h = start(net=net)
    events = config_events(h)
    res = h.post({"name": TICKETS, "config": {"refreshSeconds": 60, "mode": "full", "columns": None}})
    assert res.status_code == 201
    assert values_of(h) == {"values": {"refreshSeconds": 60, "columns": ["name", "status"], "mode": "full"}, "version": "1"}
    section = sections(h)[0]
    assert [(e["key"], e["value"], e["source"]) for e in section["entries"]] == [
        ("refreshSeconds", 60, "runtime"), ("columns", ["name", "status"], "default"), ("mode", "full", "runtime"),
    ]
    assert [(e.type, e.data, e.audience.permission) for e in events] == [(
        "config:changed",
        {
            "provider": "dashboard-plugins", "section": "tickets", "keys": ["refreshSeconds", "mode"], "version": "1",
            "requiresRestart": False, "store": "memory", "user": "admin",
        },
        "config:read",
    )]
    assert audit_of(h, "dashboard.plugin.install").properties == {
        "name": TICKETS, "version": "1.0.0", "id": "tickets", "config": ["refreshSeconds", "mode"],
    }


def test_refuses_invalid_values_with_400_per_key_before_anything_is_downloaded_or_stored(start):
    net = FakeNet()
    publish_tickets(net)
    net.publish("@acme/cli-dashboard-plain", [{"version": "1.0.0"}])
    h = start(net=net)
    events = config_events(h)
    res = h.post({"name": TICKETS, "config": {"refreshSeconds": 1, "columns": "name", "mode": "huge", "nope": True}})
    assert res.status_code == 400
    assert res.json() == {
        "error": "validation failed",
        "errors": {
            "refreshSeconds": "must be at least 5", "columns": "must be a list of strings",
            "mode": "must be one of: compact, full", "nope": "unknown setting",
        },
    }
    for config in ('{"a":1}', [1], 3):
        bad = h.post({"name": TICKETS, "config": config})
        assert bad.status_code == 400
        assert bad.json() == {"error": "config must be an object"}
    # values for a plugin without settings
    plain = h.post({"name": "@acme/cli-dashboard-plain", "config": {"a": 1}})
    assert plain.status_code == 400
    assert plain.json()["errors"] == {"a": "unknown setting"}

    assert cdn_calls(net) == []
    assert h.get(f"{BASE}/installed").json() == []
    assert sections(h) == []
    assert events == []
    assert h.audit == []


def test_a_secret_entry_or_a_malformed_config_from_the_marketplace_installs_without_settings_with_a_warning(start):
    net = FakeNet()
    net.publish("@acme/cli-dashboard-secret", [{
        "version": "1.0.0",
        "config": {"entries": [{"key": "token", "type": "secret"}, {"key": "ok", "type": "string", "default": "x"}]},
    }])
    net.publish("@acme/cli-dashboard-broken", [{
        "version": "2.0.0", "config": {"entries": [{"key": "n", "type": "number", "default": 1, "validation": {"min": 5}}]},
    }])
    net.publish("@acme/cli-dashboard-junk", [{"version": "3.0.0", "config": "junk"}])
    h = start(net=net)

    secret = h.post({"name": "@acme/cli-dashboard-secret"})
    assert secret.status_code == 201
    assert secret.json()["hasConfig"] is False
    assert secret.json()["warnings"] == [
        "the settings of @acme/cli-dashboard-secret@1.0.0 are invalid and were ignored: "
        "token: secret settings are not allowed (a dashboard plugin runs in the browser)",
    ]
    assert h.post({"name": "@acme/cli-dashboard-broken"}).json()["warnings"] == [
        "the settings of @acme/cli-dashboard-broken@2.0.0 are invalid and were ignored: n: the default must be at least 5",
    ]
    assert h.post({"name": "@acme/cli-dashboard-junk"}).json()["warnings"] == [
        "the settings of @acme/cli-dashboard-junk@3.0.0 are invalid and were ignored: config must be an object",
    ]
    assert sections(h) == []
    assert all("config" not in p for p in saved_plugins(h))
    assert h.get(f"{BASE}/installed/@acme/cli-dashboard-secret/config").json() == {"values": {}, "version": None}


def test_values_given_for_a_refused_config_are_unknown_settings(start):
    net = FakeNet()
    net.publish("@acme/cli-dashboard-secret", [{"version": "1.0.0", "config": {"entries": [{"key": "token", "type": "secret"}]}}])
    h = start(net=net)
    res = h.post({"name": "@acme/cli-dashboard-secret", "config": {"token": "abc"}})
    assert res.status_code == 400
    assert res.json() == {"error": "validation failed", "errors": {"token": "unknown setting"}}
    assert cdn_calls(net) == []


# --------------------------------------------------------------------------- update and remove


def test_update_keeps_the_stored_values_the_new_version_still_accepts_and_drops_the_others(start):
    net = FakeNet()
    publish_tickets(net, [{"version": "1.0.0", "config": SETTINGS_V1}])
    h = start(net=net)
    assert h.post({"name": TICKETS, "config": {"refreshSeconds": 60, "columns": ["id", "title"], "mode": "full"}}).status_code == 201
    publish_tickets(net, [{"version": "1.0.0", "config": SETTINGS_V1}, {"version": "2.0.0", "config": SETTINGS_V2}])
    events = config_events(h)

    res = h.put(TICKETS, {})
    assert res.status_code == 200
    body = res.json()
    assert (body["name"], body["version"], body["hasConfig"], body["configDropped"]) == (TICKETS, "2.0.0", True, ["refreshSeconds", "mode"])
    # refreshSeconds 60 is above the new max, mode is gone; columns kept; theme new at its default
    assert values_of(h) == {"values": {"refreshSeconds": 15, "columns": ["id", "title"], "theme": "light"}, "version": "2"}
    section = sections(h)[0]
    assert section["description"] == f"{TICKETS}@2.0.0"
    assert [f"{e['key']}:{e['source']}" for e in section["entries"]] == ["refreshSeconds:default", "columns:runtime", "theme:default"]
    assert saved_plugins(h)[0]["config"] == parsed(SETTINGS_V2)
    assert [e.data for e in events] == [{
        "provider": "dashboard-plugins", "section": "tickets", "keys": ["refreshSeconds", "mode"], "version": "2",
        "requiresRestart": False, "store": "memory", "user": "admin",
    }]
    assert audit_of(h, "dashboard.plugin.update").properties == {
        "name": TICKETS, "version": "2.0.0", "id": "tickets", "previousVersion": "1.0.0", "configDropped": ["refreshSeconds", "mode"],
    }


def test_nothing_to_drop_config_dropped_is_empty_and_nothing_is_written(start):
    net = FakeNet()
    publish_tickets(net, [{"version": "1.0.0", "config": SETTINGS_V1}, {"version": "1.1.0", "config": SETTINGS_V1}])
    h = start(net=net)
    assert h.post({"name": TICKETS, "version": "1.0.0", "config": {"refreshSeconds": 90}}).status_code == 201
    events = config_events(h)
    body = h.put(TICKETS, {}).json()
    assert (body["version"], body["configDropped"]) == ("1.1.0", [])
    assert values_of(h) == {"values": {"refreshSeconds": 90, "columns": ["name", "status"], "mode": "compact"}, "version": "1"}
    assert events == []


def test_values_given_with_an_update_are_checked_against_the_new_version_before_downloading_then_applied(start):
    net = FakeNet()
    publish_tickets(net, [{"version": "1.0.0", "config": SETTINGS_V1}, {"version": "2.0.0", "config": SETTINGS_V2}])
    h = start(net=net)
    assert h.post({"name": TICKETS, "version": "1.0.0", "config": {"mode": "full"}}).status_code == 201
    downloads = len(cdn_calls(net))

    # mode exists in 1.0.0 only, theme in 2.0.0 only
    bad = h.put(TICKETS, {"config": {"mode": "full", "theme": "blue"}})
    assert bad.status_code == 400
    assert bad.json() == {"error": "validation failed", "errors": {"mode": "unknown setting", "theme": "must be one of: light, dark"}}
    assert len(cdn_calls(net)) == downloads
    assert h.get(f"{BASE}/installed").json()[0]["version"] == "1.0.0"
    assert values_of(h)["values"]["mode"] == "full"
    assert h.put(TICKETS, {"config": "x"}).json() == {"error": "config must be an object"}

    ok = h.put(TICKETS, {"config": {"theme": "dark", "refreshSeconds": 20}})
    assert ok.status_code == 200
    assert (ok.json()["version"], ok.json()["configDropped"]) == ("2.0.0", ["mode"])
    assert values_of(h)["values"] == {"refreshSeconds": 20, "columns": ["name"], "theme": "dark"}
    properties = audit_of(h, "dashboard.plugin.update").properties
    assert (properties["config"], properties["configDropped"]) == (["theme", "refreshSeconds"], ["mode"])


def test_a_value_given_for_a_dropped_key_keeps_it_and_none_resets_a_setting(start):
    net = FakeNet()
    publish_tickets(net, [{"version": "1.0.0", "config": SETTINGS_V1}, {"version": "2.0.0", "config": SETTINGS_V2}])
    h = start(net=net)
    assert h.post({"name": TICKETS, "version": "1.0.0", "config": {"refreshSeconds": 600, "columns": ["a"]}}).status_code == 201
    res = h.put(TICKETS, {"config": {"refreshSeconds": 25, "columns": None}})
    assert res.json()["configDropped"] == []
    assert values_of(h)["values"] == {"refreshSeconds": 25, "columns": ["name"], "theme": "light"}


def test_the_same_version_with_values_applied_and_a_version_without_settings_removes_the_section(start):
    net = FakeNet()
    publish_tickets(net, [{"version": "1.0.0", "config": SETTINGS_V1}])
    h = start(net=net)
    assert h.post({"name": TICKETS, "config": {"mode": "full"}}).status_code == 201
    same = h.put(TICKETS, {"config": {"refreshSeconds": 45}}).json()
    assert (same["version"], same["configDropped"]) == ("1.0.0", [])
    assert {k: values_of(h)["values"][k] for k in ("refreshSeconds", "mode")} == {"refreshSeconds": 45, "mode": "full"}
    assert h.put(TICKETS, {"config": {"refreshSeconds": 1}}).json()["errors"] == {"refreshSeconds": "must be at least 5"}

    publish_tickets(net, [{"version": "1.0.0", "config": SETTINGS_V1}, {"version": "3.0.0"}])
    body = h.put(TICKETS, {}).json()
    assert (body["version"], body["hasConfig"], body["configDropped"]) == ("3.0.0", False, ["mode", "refreshSeconds"])
    assert sections(h) == []
    assert values_of(h) == {"values": {}, "version": None}


def test_removing_a_plugin_deletes_its_section_and_stored_values_installed_again_it_starts_from_the_defaults(start):
    net = FakeNet()
    publish_tickets(net)
    store = InMemoryConfigStore()
    h = start(net=net, config=CliServerConfigOptions(stores={"dashboard-plugins": store}))
    assert h.post({"name": TICKETS, "config": {"refreshSeconds": 60, "mode": "full"}}).status_code == 201
    assert asyncio.run(store.read("dashboard-plugins", "tickets")).values == {"refreshSeconds": 60, "mode": "full"}
    events = config_events(h)

    assert h.delete(TICKETS).status_code == 200
    stored = asyncio.run(store.read("dashboard-plugins", "tickets"))
    assert (stored.values, stored.version) == ({}, 2)
    assert sections(h) == []
    assert h.get(f"{BASE}/installed/{TICKETS}/config").status_code == 404
    assert h.client.patch(SECTION, headers=h.auth(), json={"changes": {"mode": "full"}}).status_code == 404
    assert [e.data for e in events] == [{
        "provider": "dashboard-plugins", "section": "tickets", "keys": ["refreshSeconds", "mode"], "version": "2",
        "requiresRestart": False, "store": "memory", "user": "admin",
    }]

    assert h.post({"name": TICKETS}).status_code == 201
    assert values_of(h) == {"values": {"refreshSeconds": 30, "columns": ["name", "status"], "mode": "compact"}, "version": "2"}


def test_values_left_in_the_store_from_an_earlier_install_of_the_page_are_cleared_on_install(start):
    net = FakeNet()
    publish_tickets(net)
    store = InMemoryConfigStore()
    asyncio.run(store.write("dashboard-plugins", "tickets", {"mode": "full", "stale": 1}))
    h = start(net=net, config=CliServerConfigOptions(stores={"dashboard-plugins": store}))
    assert h.post({"name": TICKETS, "config": {"refreshSeconds": 10}}).status_code == 201
    assert asyncio.run(store.read("dashboard-plugins", "tickets")).values == {"refreshSeconds": 10}


# --------------------------------------------------------------------------- restart and stores


def test_re_creates_the_sections_from_installed_json_after_a_restart_offline_with_the_stored_values(start, tmp_path):
    net = FakeNet()
    publish_tickets(net)
    directory = str(tmp_path / "plugins")
    file = str(tmp_path / "qcli-config.json")
    h1 = start(net=net, dir=directory, config=CliServerConfigOptions(stores={"dashboard-plugins": JsonFileConfigStore(file)}))
    assert h1.post({"name": TICKETS, "config": {"refreshSeconds": 120}}).status_code == 201
    with open(file, encoding="utf-8") as f:
        assert json.load(f)["dashboard-plugins"]["tickets"] == {"values": {"refreshSeconds": 120}, "version": 1}

    offline = FakeNet()
    offline.marketplace_down = True
    h2 = start(net=offline, dir=directory, config=CliServerConfigOptions(stores={"dashboard-plugins": JsonFileConfigStore(file)}))
    provider = h2.get(f"{CONFIG}/dashboard-plugins").json()
    assert (provider["store"]["kind"], provider["store"]["persistent"]) == ("file", True)
    assert [(s["id"], s["title"], s["version"]) for s in provider["sections"]] == [("tickets", "Tickets", "1")]
    assert values_of(h2) == {"values": {"refreshSeconds": 120, "columns": ["name", "status"], "mode": "compact"}, "version": "1"}
    # changeable after the restart, without the marketplace
    assert h2.client.patch(SECTION, headers=h2.auth(), json={"changes": {"mode": "full"}, "version": "1"}).status_code == 200
    assert offline.calls == []


def test_an_installed_json_whose_settings_became_invalid_loads_the_plugin_without_them(start, tmp_path):
    net = FakeNet()
    publish_tickets(net)
    directory = str(tmp_path / "plugins")
    first = start(net=net, dir=directory)
    assert first.post({"name": TICKETS}).status_code == 201
    path = os.path.join(directory, "installed.json")
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    saved["plugins"][0]["config"]["entries"].append({"key": "apiKey", "type": "secret"})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(saved, f)
    second = start(net=net, dir=directory)
    installed = second.get(f"{BASE}/installed").json()
    assert [(p["name"], p["hasConfig"]) for p in installed] == [(TICKETS, False)]
    assert sections(second) == []


def test_the_providers_store_the_hosts_store_for_dashboard_plugins_else_the_default_store_else_memory(start):
    net = FakeNet()
    publish_tickets(net)
    own = InMemoryConfigStore()
    fallback = InMemoryConfigStore()
    with_own = start(net=net, config=CliServerConfigOptions(store=fallback, stores={"dashboard-plugins": own}))
    assert with_own.result.builder.config_registry.store_of("dashboard-plugins") is own
    assert with_own.post({"name": TICKETS, "config": {"mode": "full"}}).status_code == 201
    assert asyncio.run(own.read("dashboard-plugins", "tickets")).values == {"mode": "full"}
    assert asyncio.run(fallback.read("dashboard-plugins", "tickets")).version == 0

    default_store = InMemoryConfigStore()
    with_default = start(net=net, config=CliServerConfigOptions(store=default_store))
    assert with_default.result.builder.config_registry.store_of("dashboard-plugins") is default_store
    assert with_default.post({"name": TICKETS, "config": {"mode": "full"}}).status_code == 201
    assert asyncio.run(default_store.read("dashboard-plugins", "tickets")).values == {"mode": "full"}

    plain = start(net=net)
    assert plain.get(f"{CONFIG}/dashboard-plugins").json()["store"] == {
        "kind": "memory", "title": "In memory", "detail": "", "persistent": False,
    }
    assert [p.id for p in plain.admin.config_providers] == ["auth", "logs", "dashboard-plugins"]


# --------------------------------------------------------------------------- through the config providers


def test_config_set_validated_by_the_existing_command_versions_audit_and_config_changed(start):
    net = FakeNet()
    publish_tickets(net)
    h = start(net=net, anonymous_admin=True)
    assert h.post({"name": TICKETS}).status_code == 201
    events = config_events(h)

    def run(line: str) -> str:
        return h.run(line).text

    assert run("config set dashboard-plugins.tickets.refreshSeconds 2") == "Error: validation failed\n  refreshSeconds: must be at least 5"
    assert run("config set dashboard-plugins.tickets.refreshSeconds soon") == "Error: validation failed\n  refreshSeconds: must be a number"
    assert run("config set dashboard-plugins.tickets.mode huge") == "Error: validation failed\n  mode: must be one of: compact, full"
    assert run("config set dashboard-plugins.tickets.nope 1") == "Error: validation failed\n  nope: unknown setting"
    assert run("config set dashboard-plugins.other.mode full") == "Error: unknown config section: dashboard-plugins.other.mode"
    assert events == []

    assert run("config set dashboard-plugins.tickets.refreshSeconds 60") == "Updated dashboard-plugins.tickets.refreshSeconds = 60 (version 1)"
    assert run("config set dashboard-plugins.tickets.columns id,title") == 'Updated dashboard-plugins.tickets.columns = ["id","title"] (version 2)'
    assert run("config set dashboard-plugins.tickets.mode full --expected-version 1") == "Error: version conflict (current version 2)"
    assert run("config get dashboard-plugins.tickets") == "\n".join([
        "dashboard-plugins.tickets  Tickets  (version 2)",
        "  refreshSeconds  60  [number, runtime]",
        '  columns         ["id","title"]  [string[], runtime]',
        "  mode            compact  [enum, default]",
    ])
    assert [(e.type, e.data) for e in events] == [
        ("config:changed", {
            "provider": "dashboard-plugins", "section": "tickets", "keys": ["refreshSeconds"], "version": "1",
            "requiresRestart": False, "store": "memory",
        }),
        ("config:changed", {
            "provider": "dashboard-plugins", "section": "tickets", "keys": ["columns"], "version": "2",
            "requiresRestart": False, "store": "memory",
        }),
    ]
    assert [(a.target, a.properties["changes"]) for a in h.audit if a.event == "config.update"] == [
        ("config:dashboard-plugins.tickets", [{"key": "refreshSeconds", "old": 30, "new": 60}]),
        ("config:dashboard-plugins.tickets", [{"key": "columns", "old": ["name", "status"], "new": ["id", "title"]}]),
    ]
    # the page's values follow
    assert values_of(h) == {"values": {"refreshSeconds": 60, "columns": ["id", "title"], "mode": "compact"}, "version": "2"}
    # reset to the default
    assert "dashboard-plugins.tickets.refreshSeconds" in run("config reset dashboard-plugins.tickets.refreshSeconds")
    assert values_of(h)["values"]["refreshSeconds"] == 30


def test_patch_config_dashboard_plugins_400_per_key_409_on_a_stale_version(start):
    net = FakeNet()
    publish_tickets(net)
    h = start(net=net)
    assert h.post({"name": TICKETS}).status_code == 201
    bad = h.client.patch(SECTION, headers=h.auth(), json={"changes": {"refreshSeconds": 9999, "columns": [1]}})
    assert bad.status_code == 400
    assert bad.json() == {"error": "validation failed", "errors": {"refreshSeconds": "must be at most 3600", "columns": "must be a list of strings"}}
    ok = h.client.patch(SECTION, headers=h.auth(), json={"changes": {"mode": "full"}, "version": "0"})
    assert ok.status_code == 200
    assert (ok.json()["provider"], ok.json()["applied"], ok.json()["version"]) == ("dashboard-plugins", ["mode"], "1")
    stale = h.client.patch(SECTION, headers=h.auth(), json={"changes": {"mode": "compact"}, "version": "0"})
    assert stale.status_code == 409
    assert stale.json() == {"error": "version conflict", "version": "1"}


# --------------------------------------------------------------------------- permissions on a protected server


def test_values_need_admin_access_changes_config_write_install_time_values_dashboard_install(start):
    h = start(users={
        "viewer": ["admin:access"],
        "reader": ["admin:access", "config:read"],
        "writer": ["admin:access", "config:read", "config:write"],
        "installer": ["admin:access", "dashboard:install"],
        "outsider": ["files:read"],
    })
    publish_tickets(h.net)

    def install(user: str) -> Any:
        return h.post({"name": TICKETS, "config": {"refreshSeconds": 60}}, user)

    # install-time values: dashboard:install, not config:write
    assert install("writer").status_code == 403
    installed = install("installer")
    assert installed.status_code == 201
    assert installed.json()["installedBy"] == "installer"

    # the values: admin:access
    assert values_of(h, "viewer")["values"]["refreshSeconds"] == 60
    assert h.get(f"{BASE}/installed/{TICKETS}/config", "outsider").status_code == 403
    assert h.client.get(f"{BASE}/installed/{TICKETS}/config").status_code == 401

    # the config API: config:read to see, config:write to change
    assert h.get(f"{CONFIG}/dashboard-plugins", "viewer").status_code == 403
    assert h.get(f"{CONFIG}/dashboard-plugins", "reader").json()["writable"] is False
    assert h.client.patch(SECTION, headers=h.auth("reader"), json={"changes": {"mode": "full"}}).status_code == 403
    assert h.client.patch(SECTION, headers=h.auth("installer"), json={"changes": {"mode": "full"}}).status_code == 403
    assert h.client.patch(SECTION, headers=h.auth("writer"), json={"changes": {"mode": "full"}}).status_code == 200
    values = values_of(h, "viewer")["values"]
    assert (values["refreshSeconds"], values["mode"]) == (60, "full")

    # updating with values: dashboard:install
    assert h.put(TICKETS, {"config": {"mode": "compact"}}, "writer").status_code == 403
    assert h.put(TICKETS, {"config": {"mode": "compact"}}, "installer").status_code == 200


def test_the_command_install_and_update_config_need_dashboard_install_info_admin_access(start):
    h = start(users={
        "viewer": ["admin:access", "commands:execute"],
        "installer": ["admin:access", "commands:execute", "dashboard:install"],
    })
    publish_tickets(h.net)
    dashboard = next(p for p in h.result.registry.find_processor("admin").processors if p.command == "dashboard")
    assert not next(p for p in dashboard.processors if p.command == "info").required_permissions
    assert h.run(f"admin dashboard install {TICKETS} --config '{{\"mode\":\"full\"}}'", "viewer").status == 403
    assert f"Installed {TICKETS}@1.0.0" in h.run(f"admin dashboard install {TICKETS} --config '{{\"mode\":\"full\"}}'", "installer").text
    assert "mode            full *" in h.run(f"admin dashboard info {TICKETS}", "viewer").text
    assert h.run(f"admin dashboard update {TICKETS} --config '{{\"mode\":\"compact\"}}'", "viewer").status == 403


# --------------------------------------------------------------------------- the admin dashboard command


def test_install_config_applied_per_key_errors_listed_a_bad_config_refused(start):
    net = FakeNet()
    publish_tickets(net)
    net.publish("@acme/cli-dashboard-secret", [{"version": "1.0.0", "config": {"entries": [{"key": "token", "type": "secret"}]}}])
    h = start(net=net, anonymous_admin=True)

    def run(line: str) -> str:
        return h.run(line).text

    assert run(f"admin dashboard install {TICKETS} --config '{{\"refreshSeconds\":1,\"mode\":\"huge\",\"nope\":1}}'") == "\n".join([
        "Error: validation failed",
        "  refreshSeconds: must be at least 5",
        "  mode: must be one of: compact, full",
        "  nope: unknown setting",
    ])
    invalid = "Error: invalid --config: use a JSON object, e.g. --config '{\"key\":\"value\"}'"
    assert run(f"admin dashboard install {TICKETS} --config 'not json'") == invalid
    assert run(f"admin dashboard install {TICKETS} --config '[1]'") == invalid
    assert cdn_calls(net) == []

    assert run(f"admin dashboard install {TICKETS} --config '{{\"refreshSeconds\":60,\"columns\":[\"id\"]}}'") == "\n".join([
        f'Installed {TICKETS}@1.0.0: the dashboard page "Tickets" (/x/tickets).',
        f"Its settings: admin dashboard info {TICKETS}",
    ])
    assert values_of(h)["values"] == {"refreshSeconds": 60, "columns": ["id"], "mode": "compact"}

    assert run("admin dashboard install @acme/cli-dashboard-secret") == "\n".join([
        'Installed @acme/cli-dashboard-secret@1.0.0: the dashboard page "cli-dashboard-secret" (/x/secret).',
        "Warning: the settings of @acme/cli-dashboard-secret@1.0.0 are invalid and were ignored: "
        "token: secret settings are not allowed (a dashboard plugin runs in the browser)",
    ])


def test_info_the_page_files_and_settings_with_their_values_defaults_and_rules(start):
    net = FakeNet()
    publish_tickets(net)
    net.publish("@acme/cli-dashboard-plain", [{"version": "1.0.0", "css": None}])
    h = start(net=net, anonymous_admin=True)

    def run(line: str) -> str:
        return h.run(line).text

    run(f"admin dashboard install {TICKETS} --config '{{\"refreshSeconds\":60}}'")
    run("admin dashboard install @acme/cli-dashboard-plain")

    info = run(f"admin dashboard info {TICKETS}").split("\n")
    assert info[0] == f"{TICKETS}@1.0.0"
    assert info[1] == "  Page        Tickets (/x/tickets)"
    assert re.fullmatch(r"  Installed   \d{4}-\d\d-\d\d \d\d:\d\d:\d\d by anonymous", info[2])
    assert re.fullmatch(r"  Files       extension\.js \(\d+ bytes\), extension\.css \(\d+ bytes\)", info[3])
    assert info[4:] == [
        "Settings (config set dashboard-plugins.tickets.<key> <value>; version 1):",
        "Key             Value              Type      Default            Rule",
        "refreshSeconds  60 *               number    30                 min 5; max 3600",
        'columns         ["name","status"]  string[]  ["name","status"]  -',
        "mode            compact            enum      compact            one of compact, full",
        "* changed from the default",
    ]
    plain = run("admin dashboard info @acme/cli-dashboard-plain").split("\n")
    assert re.fullmatch(r"  Files       extension\.js \(\d+ bytes\)", plain[3])
    assert plain[4] == "  Settings    none"

    data = json.loads(run(f"admin dashboard info {TICKETS} --json"))
    assert (data["plugin"]["name"], data["plugin"]["id"], data["plugin"]["hasConfig"], data["configVersion"]) == (TICKETS, "tickets", True, "1")
    assert data["settings"][0] == {
        "key": "refreshSeconds", "label": "Refresh every", "description": "Seconds between two refreshes", "type": "number",
        "default": 30, "validation": {"min": 5, "max": 3600}, "value": 60, "isDefault": False,
    }

    assert run("admin dashboard info @acme/none") == "Error: not installed: @acme/none"
    assert run("admin dashboard info ../x") == "Error: invalid package name"
    assert run("admin dashboard info") == "Error: name one package: admin dashboard info <name>"


def test_update_config_and_the_settings_it_drops_reported(start):
    net = FakeNet()
    publish_tickets(net, [{"version": "1.0.0", "config": SETTINGS_V1}])
    h = start(net=net, anonymous_admin=True)

    def run(line: str) -> str:
        return h.run(line).text

    run(f"admin dashboard install {TICKETS} --config '{{\"refreshSeconds\":120,\"mode\":\"full\"}}'")
    assert run(f"admin dashboard update {TICKETS} --config '{{\"mode\":\"compact\"}}'") == (
        f"{TICKETS} is already at 1.0.0, the newest version. Its settings were changed."
    )
    assert run(f"admin dashboard update {TICKETS} --config '{{\"mode\":\"x\"}}'") == "Error: validation failed\n  mode: must be one of: compact, full"

    publish_tickets(net, [{"version": "1.0.0", "config": SETTINGS_V1}, {"version": "2.0.0", "config": SETTINGS_V2}])
    assert run(f"admin dashboard update {TICKETS} --config '{{\"theme\":\"dark\"}}'") == "\n".join([
        f"Updated {TICKETS} from 1.0.0 to 2.0.0.",
        "Settings dropped (the new version no longer accepts them): refreshSeconds, mode",
        f"Its settings: admin dashboard info {TICKETS}",
    ])
    assert values_of(h)["values"] == {"refreshSeconds": 15, "columns": ["name"], "theme": "dark"}
