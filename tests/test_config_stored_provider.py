"""CliStoredConfigProvider (settings described in code, changes in a store) and the custom provider.

Ported from cli-server-node ``src/__tests__/config-stored-provider.test.ts``.  Node's
``attachStore()`` + ``ready()`` pair is one awaited ``attach_store()`` in Python."""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any, Callable, Optional
from unittest.mock import Mock

import pytest

from qodalis_cli_server_abstractions.config import (
    CliConfigConflictError,
    CliConfigEntry,
    CliConfigSectionDefinition,
    CliConfigSettingDefinition,
    CliConfigStoredSection,
    CliConfigUpdateResult,
    CliConfigValidation,
    CliStoredConfigProvider,
    ICliConfigStore,
    InMemoryConfigStore,
    create_custom_config_provider,
)


def billing(apply: Optional[dict[str, Callable[[Any], None]]] = None) -> CliStoredConfigProvider:
    apply = apply or {}
    return CliStoredConfigProvider(
        id="billing",
        title="Billing",
        description="Invoices",
        order=5,
        required_permission="billing:admin",
        sections=[
            CliConfigSectionDefinition(
                id="invoices",
                title="Invoices",
                settings=[
                    CliConfigSettingDefinition(
                        key="dueDays", label="Due in", type="number", default=30,
                        validation=CliConfigValidation(min=1, max=90), apply=apply.get("dueDays"),
                    ),
                    CliConfigSettingDefinition(key="apiKey", type="secret", apply=apply.get("apiKey")),
                    CliConfigSettingDefinition(key="endpoint", type="url", default="https://pay.example", sensitive=True),
                    CliConfigSettingDefinition(
                        key="currency", type="enum", default="EUR",
                        validation=CliConfigValidation(options=["EUR", "USD"]), source="environment",
                    ),
                    CliConfigSettingDefinition(
                        key="region", type="string", mutable=False, value=lambda: "eu-1", requires_restart=True,
                    ),
                    CliConfigSettingDefinition(
                        key="even", type="number", default=2,
                        validate=lambda v: None if v % 2 == 0 else "must be even",
                    ),
                ],
            ),
            CliConfigSectionDefinition(
                id="info",
                settings=[CliConfigSettingDefinition(key="build", type="string", mutable=False, default="1.2.3")],
            ),
        ],
    )


def entry(section: Any, key: str) -> CliConfigEntry:
    return next(e for e in section.entries if e.key == key)


def as_json(value: Any) -> str:
    return json.dumps(dataclasses.asdict(value))


# ---------------------------------------------------------------------------
# definition
# ---------------------------------------------------------------------------


class TestDefinition:
    def test_refuses_invalid_or_duplicate_sections_and_keys(self):
        def make(sections: list[CliConfigSectionDefinition]) -> None:
            CliStoredConfigProvider(id="x", title="X", sections=sections)

        S, K = CliConfigSectionDefinition, CliConfigSettingDefinition
        with pytest.raises(ValueError, match=re.escape("invalid or duplicate config section: a")):
            make([S(id="a", settings=[]), S(id="a", settings=[])])
        with pytest.raises(ValueError, match=re.escape("invalid or duplicate config section")):
            make([S(id="bad id", settings=[])])
        with pytest.raises(ValueError, match=re.escape("invalid or duplicate config key: a.k")):
            make([S(id="a", settings=[K(key="k", type="string"), K(key="k", type="string")])])
        with pytest.raises(ValueError, match=re.escape("invalid or duplicate config key")):
            make([S(id="a", settings=[K(key="", type="string")])])

    def test_exposes_id_title_description_order_required_permission(self):
        p = billing()
        assert [p.id, p.title, p.description, p.order, p.required_permission] == [
            "billing", "Billing", "Invoices", 5, "billing:admin",
        ]


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


class TestReading:
    async def test_entries_carry_defaults_sources_flags_rules_read_only_live(self):
        p = billing()
        invoices, info = await p.get_sections()
        assert invoices.id == "invoices"
        assert invoices.title == "Invoices"
        assert invoices.version == "0"
        assert entry(invoices, "dueDays") == CliConfigEntry(
            key="dueDays",
            label="Due in",
            type="number",
            value=30,
            default=30,
            source="default",
            mutable=True,
            requires_restart=False,
            sensitive=False,
            validation=CliConfigValidation(min=1, max=90),
        )
        assert entry(invoices, "dueDays").validation.to_dict() == {"min": 1, "max": 90}
        assert entry(invoices, "currency").source == "environment"
        region = entry(invoices, "region")
        assert (region.value, region.mutable, region.requires_restart) == ("eu-1", False, True)
        # A section without a mutable setting has no version
        assert info.version is None
        build = info.entries[0]
        assert (build.key, build.value, build.mutable) == ("build", "1.2.3", False)

    async def test_never_returns_secret_or_sensitive_value_only_is_set(self):
        p = billing()
        await p.update("invoices", {"apiKey": "sk_live_123"}, None)
        invoices, _ = await p.get_sections()
        api_key = entry(invoices, "apiKey")
        assert (api_key.value, api_key.default, api_key.is_set, api_key.sensitive) == (None, None, True, True)
        endpoint = entry(invoices, "endpoint")
        assert (endpoint.value, endpoint.default, endpoint.is_set, endpoint.sensitive) == (None, None, True, True)
        assert "sk_live_123" not in as_json(invoices)
        assert "pay.example" not in as_json(invoices)
        # The host's code reads it
        assert p.value("invoices", "apiKey") == "sk_live_123"

    async def test_value_stored_else_default_read_only_live_unknown_none(self):
        p = billing()
        assert p.value("invoices", "dueDays") == 30
        await p.update("invoices", {"dueDays": 45}, None)
        assert p.value("invoices", "dueDays") == 45
        assert p.value("invoices", "region") == "eu-1"
        assert p.value("invoices", "nope") is None
        assert p.value("nope", "dueDays") is None
        assert p.value("info", "build") == "1.2.3"


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_unknown_keys_read_only_keys_and_custom_rules(self):
        p = billing()
        assert p.validate("invoices", {"nope": 1, "region": "x", "even": 3, "dueDays": 10}) == {
            "nope": "unknown setting",
            "region": "read-only",
            "even": "must be even",
        }
        assert p.validate("invoices", {"even": None}) == {}
        assert p.validate("missing", {"a": 1}) == {}


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


class TestUpdate:
    async def test_stores_bumps_version_applies_effective_value_reports_restart(self):
        due_days = Mock()
        p = billing({"dueDays": due_days})
        result = await p.update("invoices", {"dueDays": 60}, "0")
        assert result == CliConfigUpdateResult(applied=["dueDays"], requires_restart=False, version="1")
        due_days.assert_called_with(60)
        invoices, _ = await p.get_sections()
        assert invoices.version == "1"
        due = entry(invoices, "dueDays")
        assert (due.value, due.source) == (60, "runtime")

    async def test_none_resets_to_default_and_applies_it(self):
        due_days = Mock()
        p = billing({"dueDays": due_days})
        await p.update("invoices", {"dueDays": 60}, None)
        await p.update("invoices", {"dueDays": None}, None)
        assert due_days.call_args.args == (30,)
        invoices, _ = await p.get_sections()
        due = entry(invoices, "dueDays")
        assert (due.value, due.source) == (30, "default")

    async def test_secret_replaced_and_cleared_never_read_back(self):
        api_key = Mock()
        p = billing({"apiKey": api_key})
        await p.update("invoices", {"apiKey": "one"}, None)
        await p.update("invoices", {"apiKey": "two"}, None)
        assert p.value("invoices", "apiKey") == "two"
        await p.update("invoices", {"apiKey": None}, None)
        assert api_key.call_args.args == (None,)
        invoices, _ = await p.get_sections()
        secret = entry(invoices, "apiKey")
        assert (secret.value, secret.is_set) == (None, False)

    async def test_stale_or_malformed_version_is_conflict_with_current_version(self):
        p = billing()
        await p.update("invoices", {"dueDays": 10}, "0")
        with pytest.raises(CliConfigConflictError) as stale:
            await p.update("invoices", {"dueDays": 11}, "0")
        assert stale.value.current_version == "1"
        with pytest.raises(CliConfigConflictError) as bad:
            await p.update("invoices", {"dueDays": 11}, "abc")
        assert bad.value.current_version == "1"
        assert p.value("invoices", "dueDays") == 10

    async def test_requires_restart_when_changed_setting_needs_one(self):
        p = CliStoredConfigProvider(
            id="x",
            title="X",
            sections=[CliConfigSectionDefinition(id="s", settings=[
                CliConfigSettingDefinition(key="port", type="number", default=1, requires_restart=True),
                CliConfigSettingDefinition(key="b", type="boolean", default=False),
            ])],
        )
        assert (await p.update("s", {"b": True}, None)).requires_restart is False
        assert (await p.update("s", {"port": 2, "b": False}, None)).requires_restart is True

    async def test_unknown_section_raises(self):
        with pytest.raises(Exception, match=re.escape("unknown config section: billing.nope")):
            await billing().update("nope", {"a": 1}, None)


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


class _FailingStore(ICliConfigStore):
    source = "database"

    async def read(self, provider: str, section: str) -> CliConfigStoredSection:
        raise RuntimeError("db down")

    async def write(self, provider, section, changes, expected_version=None) -> CliConfigStoredSection:
        raise RuntimeError("db down")


class TestStore:
    async def test_attach_store_applies_stored_values_mutable_only_and_uses_store_source(self):
        store = InMemoryConfigStore()
        await store.write("billing", "invoices", {"dueDays": 14, "region": "stored-but-read-only", "apiKey": "k"})
        due_days, api_key = Mock(), Mock()
        p = billing({"dueDays": due_days, "apiKey": api_key})
        await p.attach_store(store)
        due_days.assert_called_with(14)
        api_key.assert_called_with("k")
        invoices, _ = await p.get_sections()
        assert invoices.version == "1"
        due = entry(invoices, "dueDays")
        assert (due.value, due.source) == (14, "runtime")
        assert entry(invoices, "region").value == "eu-1"

    async def test_two_providers_on_one_store_changes_reach_the_store(self):
        store = InMemoryConfigStore()
        a = billing()
        await a.attach_store(store)
        await a.update("invoices", {"dueDays": 7}, None)
        assert await store.read("billing", "invoices") == CliConfigStoredSection({"dueDays": 7}, 1)
        # A second instance (a restart) sees it
        b = billing()
        await b.attach_store(store)
        assert b.value("invoices", "dueDays") == 7

    async def test_failing_store_rejects_attach(self):
        p = billing()
        with pytest.raises(RuntimeError, match="db down"):
            await p.attach_store(_FailingStore())

    async def test_without_attach_store_keeps_changes_in_memory(self):
        p = billing()
        assert p.store.source == "runtime"
        await p.update("invoices", {"dueDays": 3}, None)
        assert p.value("invoices", "dueDays") == 3


# ---------------------------------------------------------------------------
# create_custom_config_provider
# ---------------------------------------------------------------------------


class TestCustomProvider:
    async def test_one_free_section_keys_added_inferred_and_removed_with_none(self):
        p = create_custom_config_provider()
        assert [p.id, p.title, p.order] == ["custom", "Custom", 1000]
        [custom] = await p.get_sections()
        assert (custom.id, custom.allow_custom_keys, custom.version, custom.entries) == ("custom", True, "0", [])

        await p.update(
            "custom", {"motd": "hello", "retries": 3, "beta": True, "tags": ["a"], "limits": {"a": 1}}, "0",
        )
        [custom] = await p.get_sections()
        assert [[e.key, e.type, e.value, e.mutable, e.source] for e in custom.entries] == [
            ["motd", "string", "hello", True, "runtime"],
            ["retries", "number", 3, True, "runtime"],
            ["beta", "boolean", True, True, "runtime"],
            ["tags", "string[]", ["a"], True, "runtime"],
            ["limits", "json", {"a": 1}, True, "runtime"],
        ]
        assert p.value("custom", "motd") == "hello"

        await p.update("custom", {"motd": None}, "1")
        [custom] = await p.get_sections()
        assert [e.key for e in custom.entries] == ["retries", "beta", "tags", "limits"]
        assert p.value("custom", "motd") is None

    def test_refuses_keys_that_are_not_identifiers(self):
        p = create_custom_config_provider()
        assert p.validate("custom", {"bad key": 1, "ok.key-1_x": 2}) == {
            "bad key": "invalid key: use letters, digits, _, - and .",
        }

    def test_options_id_title_description_order(self):
        p = create_custom_config_provider(id="app", title="App", description="mine", order=7)
        assert [p.id, p.title, p.description, p.order] == ["app", "App", "mine", 7]
