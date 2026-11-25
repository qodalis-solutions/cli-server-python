"""Config providers: value validation (every type and rule), terminal input parsing,
durations, type inference, permission matching and contexts.

Ported from cli-server-node ``src/__tests__/config-validation.test.ts``."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from qodalis_cli_server_abstractions.config import (
    CLI_CONFIG_ENTRY_TYPES,
    CliConfigCaller,
    CliConfigEntry,
    CliConfigError,
    cli_config_context,
    cli_config_values_equal,
    cli_permission_matches,
    clone_cli_config_value,
    format_cli_config_duration,
    has_cli_permission,
    infer_cli_config_entry_type,
    is_cli_config_writer,
    parse_cli_config_duration,
    parse_cli_config_input,
    validate_cli_config_value,
)

DURATION_ERROR = "must be a duration such as 500ms, 30s, 15m, 12h or 7d"


def v(entry_type: str, value: Any, validation: Any = None, default: Any = None) -> Any:
    entry = CliConfigEntry(key="k", type=entry_type, validation=validation, default=default)
    return validate_cli_config_value(entry, value)


# ---------------------------------------------------------------------------
# validate_cli_config_value: types
# ---------------------------------------------------------------------------


class TestValidateTypes:
    def test_string(self):
        assert v("string", "x") is None
        assert v("string", "") is None
        assert v("string", 1) == "must be a string"
        assert v("string", ["a"]) == "must be a string"

    def test_secret(self):
        assert v("secret", "s3cr3t") is None
        assert v("secret", 42) == "must be a string"

    def test_number(self):
        assert v("number", 0) is None
        assert v("number", -1.5) is None
        assert v("number", "1") == "must be a number"
        assert v("number", float("nan")) == "must be a number"
        assert v("number", float("inf")) == "must be a number"
        # A bool is not a number (JSON-like values)
        assert v("number", True) == "must be a number"

    def test_boolean(self):
        assert v("boolean", True) is None
        assert v("boolean", False) is None
        assert v("boolean", "true") == "must be true or false"
        assert v("boolean", 1) == "must be true or false"

    def test_string_list(self):
        assert v("string[]", []) is None
        assert v("string[]", ["a", "b"]) is None
        assert v("string[]", "a") == "must be a list of strings"
        assert v("string[]", ["a", 1]) == "must be a list of strings"

    def test_enum(self):
        rules = {"options": ["a", "b"]}
        assert v("enum", "a", rules) is None
        assert v("enum", "c", rules) == "must be one of: a, b"
        assert v("enum", 1, rules) == "must be a string"
        assert v("enum", "a") == "must be one of: "

    def test_json_any_json_value(self):
        for value in [{"a": [1, {"b": None}]}, [1, "x"], "text", 3, True]:
            assert v("json", value) is None
        assert v("json", {"a": lambda: 1}) == "must be a JSON value"
        assert v("json", {"a": float("nan")}) == "must be a JSON value"
        assert v("json", datetime.now()) == "must be a JSON value"
        assert v("json", object()) == "must be a JSON value"

    def test_duration_integer_and_unit(self):
        for value in ["500ms", "30s", "15m", "12h", "7d", "0s"]:
            assert v("duration", value) is None
        for value in ["1.5h", "10", "h", "5w", "-1s", "1h30m", 60_000]:
            assert v("duration", value) == DURATION_ERROR

    def test_url_absolute_with_scheme(self):
        assert v("url", "https://example.com/path?q=1") is None
        assert v("url", "postgres://db:5432/x") is None
        assert v("url", "") is None
        assert v("url", "/relative") == "must be an absolute URL"
        assert v("url", "mailto:a@b.c") == "must be an absolute URL"
        assert v("url", "not a url") == "must be an absolute URL"
        assert v("url", 5) == "must be a string"

    def test_unknown_type_is_refused(self):
        assert v("color", "red") == "unknown type color"

    def test_covers_every_entry_type(self):
        assert list(CLI_CONFIG_ENTRY_TYPES) == [
            "string", "number", "boolean", "string[]", "enum", "secret", "json", "duration", "url",
        ]


# ---------------------------------------------------------------------------
# validate_cli_config_value: rules
# ---------------------------------------------------------------------------


class TestValidateRules:
    def test_min_max_string_length(self):
        rules = {"min": 2, "max": 4}
        assert v("string", "a", rules) == "must be at least 2 characters"
        assert v("string", "abcde", rules) == "must be at most 4 characters"
        assert v("string", "abc", rules) is None

    def test_min_max_secret_and_url_length(self):
        assert v("secret", "short", {"min": 8}) == "must be at least 8 characters"
        assert v("url", "https://example.com", {"max": 10}) == "must be at most 10 characters"

    def test_min_max_number_value(self):
        rules = {"min": 1, "max": 10}
        assert v("number", 0, rules) == "must be at least 1"
        assert v("number", 11, rules) == "must be at most 10"
        assert v("number", 1, rules) is None
        assert v("number", 10, rules) is None

    def test_min_max_string_list_item_count(self):
        rules = {"min": 1, "max": 2}
        assert v("string[]", [], rules) == "must have at least 1 items"
        assert v("string[]", ["a", "b", "c"], rules) == "must have at most 2 items"

    def test_min_max_duration_in_milliseconds(self):
        rules = {"min": 60_000, "max": 86_400_000}
        assert v("duration", "30s", rules) == "must be at least 1m"
        assert v("duration", "2d", rules) == "must be at most 1d"
        assert v("duration", "1h", rules) is None

    def test_pattern_string_items_url_secret(self):
        assert v("string", "abc", {"pattern": "^[a-z]+$"}) is None
        assert v("string", "ABC", {"pattern": "^[a-z]+$"}) == "must match ^[a-z]+$"
        assert v("string[]", ["1", "x"], {"pattern": "^\\d+$"}) == "'x' must match ^\\d+$"
        assert v("url", "http://a.b", {"pattern": "^https:"}) == "must match ^https:"
        assert v("secret", "pk_1", {"pattern": "^sk_"}) == "must match ^sk_"

    def test_invalid_pattern_never_matches(self):
        assert v("string", "x", {"pattern": "("}) == "must match ("

    def test_options_string_and_each_item(self):
        assert v("string", "z", {"options": ["x", "y"]}) == "must be one of: x, y"
        assert v("string", "x", {"options": ["x", "y"]}) is None
        assert v("string[]", ["x", "z"], {"options": ["x", "y"]}) == "'z' must be one of: x, y"

    def test_required_refuses_empty_values(self):
        assert v("string", "", {"required": True}) == "is required"
        assert v("string[]", [], {"required": True}) == "is required"
        assert v("secret", "", {"required": True}) == "is required"
        assert v("string", "x", {"required": True}) is None

    def test_none_resets_unless_required_without_default(self):
        assert v("string", None) is None
        assert v("string", None, {"required": True}) == "is required"
        assert v("string", None, {"required": True}, default="d") is None
        assert v("number", None, {"min": 5}) is None


# ---------------------------------------------------------------------------
# parse_cli_config_input (terminal values)
# ---------------------------------------------------------------------------


class TestParseInput:
    def test_number(self):
        assert parse_cli_config_input("number", " 42 ") == 42
        assert parse_cli_config_input("number", "-1.5") == -1.5
        with pytest.raises(ValueError, match="must be a number"):
            parse_cli_config_input("number", "abc")
        with pytest.raises(ValueError, match="must be a number"):
            parse_cli_config_input("number", "")

    def test_boolean(self):
        for text in ["true", "YES", "on", "1"]:
            assert parse_cli_config_input("boolean", text) is True
        for text in ["false", "No", "off", "0"]:
            assert parse_cli_config_input("boolean", text) is False
        with pytest.raises(ValueError, match="must be true or false"):
            parse_cli_config_input("boolean", "maybe")

    def test_string_list_comma_separated_or_json_array(self):
        assert parse_cli_config_input("string[]", "a, b ,c") == ["a", "b", "c"]
        assert parse_cli_config_input("string[]", '["a,b", "c"]') == ["a,b", "c"]
        assert parse_cli_config_input("string[]", "") == []
        assert parse_cli_config_input("string[]", "a,,b") == ["a", "b"]
        with pytest.raises(ValueError, match="must be a JSON array or a comma-separated list"):
            parse_cli_config_input("string[]", "[oops")
        with pytest.raises(ValueError):
            parse_cli_config_input("string[]", "[")

    def test_json(self):
        assert parse_cli_config_input("json", '{"a":1}') == {"a": 1}
        assert parse_cli_config_input("json", "null") is None
        with pytest.raises(ValueError, match="must be JSON"):
            parse_cli_config_input("json", "{a")

    def test_everything_else_as_typed(self):
        assert parse_cli_config_input("string", " spaced ") == " spaced "
        assert parse_cli_config_input("secret", "p@ss word") == "p@ss word"
        assert parse_cli_config_input("duration", "30m") == "30m"
        assert parse_cli_config_input("enum", "a") == "a"
        assert parse_cli_config_input("url", "https://x") == "https://x"


# ---------------------------------------------------------------------------
# durations
# ---------------------------------------------------------------------------


class TestDurations:
    def test_parses_one_unit(self):
        assert parse_cli_config_duration("500ms") == 500
        assert parse_cli_config_duration("30s") == 30_000
        assert parse_cli_config_duration("15m") == 900_000
        assert parse_cli_config_duration("2h") == 7_200_000
        assert parse_cli_config_duration("7d") == 604_800_000
        assert parse_cli_config_duration(" 1h ") == 3_600_000
        assert parse_cli_config_duration("1h30m") is None
        assert parse_cli_config_duration(1000) is None

    def test_formats_in_largest_unit_that_divides(self):
        assert format_cli_config_duration(0) == "0s"
        assert format_cli_config_duration(1500) == "1500ms"
        assert format_cli_config_duration(90_000) == "90s"
        assert format_cli_config_duration(1_800_000) == "30m"
        assert format_cli_config_duration(86_400_000) == "1d"
        with pytest.raises(ValueError):
            format_cli_config_duration(-1)

    def test_round_trips(self):
        for text in ["250ms", "45s", "5m", "6h", "30d"]:
            ms = parse_cli_config_duration(text)
            assert ms is not None
            assert format_cli_config_duration(ms) == text


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_infers_type_of_free_value(self):
        assert infer_cli_config_entry_type(True) == "boolean"
        assert infer_cli_config_entry_type(3) == "number"
        assert infer_cli_config_entry_type(["a"]) == "string[]"
        assert infer_cli_config_entry_type([1]) == "json"
        assert infer_cli_config_entry_type({"a": 1}) == "json"
        assert infer_cli_config_entry_type("x") == "string"
        assert infer_cli_config_entry_type(None) == "string"

    def test_clones_and_compares_json_values(self):
        value = {"a": [1, 2]}
        copy = clone_cli_config_value(value)
        assert copy == value
        assert copy is not value
        assert copy["a"] is not value["a"]
        assert clone_cli_config_value("x") == "x"
        assert cli_config_values_equal(["a"], ["a"]) is True
        assert cli_config_values_equal(["a"], ["b"]) is False
        assert cli_config_values_equal(1, 1) is True
        assert cli_config_values_equal(None, {}) is False
        # bool is not a number
        assert cli_config_values_equal(True, 1) is False

    def test_permission_matching_wildcards_case_insensitive(self):
        assert cli_permission_matches("*", "config:write") is True
        assert cli_permission_matches("config:*", "config:read") is True
        assert cli_permission_matches("*:read", "config:read") is True
        assert cli_permission_matches("*:read", "config:write") is False
        assert cli_permission_matches("CONFIG:READ", "config:read") is True
        assert cli_permission_matches("config", "config:read") is False
        assert has_cli_permission(["files:read", "config:read"], "config:read") is True
        assert has_cli_permission(None, "config:read") is False

    def test_contexts_without_caller_everything_allowed(self):
        assert cli_config_context(None).has_permission("config:write") is True
        ctx = cli_config_context(CliConfigCaller(user_name="ann", permissions=("config:read",)), "api")
        assert ctx.has_permission("config:read") is True
        assert ctx.has_permission("config:write") is False
        assert ctx.via == "api"
        assert ctx.caller is not None and ctx.caller.user_name == "ann"
        assert cli_config_context(CliConfigCaller(user_name="bob")).has_permission("config:read") is False

    def test_writers_and_errors_recognised_by_shape(self):
        class Writer:
            def validate(self, *args):
                return {}

            async def update(self, *args):
                return {}

        class ReadOnly:
            def get_sections(self, *args):
                return []

        assert is_cli_config_writer(Writer()) is True
        assert is_cli_config_writer(ReadOnly()) is False
        assert is_cli_config_writer(None) is False
        err = CliConfigError(409, "version conflict", None, "4")
        assert isinstance(err, CliConfigError)
        assert err.to_dict() == {"error": "version conflict", "version": "4"}
        assert CliConfigError(400, "validation failed", {"a": "bad"}).to_dict() == {
            "error": "validation failed",
            "errors": {"a": "bad"},
        }
        assert not isinstance(Exception("x"), CliConfigError)

