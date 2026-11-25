"""Validation and parsing of config values: the same rules on every server."""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from .config_types import CliConfigValidation, CliConfigValue

_DURATION = re.compile(r"^(\d+)(ms|s|m|h|d)$")
_DURATION_MS = {"ms": 1, "s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
_URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def parse_cli_config_duration(value: Any) -> Optional[int]:
    """A ``duration`` setting's value in milliseconds: a whole number and one
    unit, ``ms``, ``s``, ``m``, ``h`` or ``d`` (``500ms``, ``30s``, ``15m``,
    ``12h``, ``7d``); ``0s`` is zero.  ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    match = _DURATION.match(value.strip())
    return int(match.group(1)) * _DURATION_MS[match.group(2)] if match else None


def format_cli_config_duration(ms: float) -> str:
    """Milliseconds as a duration in the largest unit that divides them (``90000`` -> ``90s``, ``3600000`` -> ``1h``)."""
    if isinstance(ms, bool) or not isinstance(ms, (int, float)) or not math.isfinite(ms) or ms < 0:
        raise ValueError(f"invalid duration: {ms}")
    whole = int(round(ms))
    if whole == 0:
        return "0s"
    for unit in ("d", "h", "m", "s"):
        if whole % _DURATION_MS[unit] == 0:
            return f"{whole // _DURATION_MS[unit]}{unit}"
    return f"{whole}ms"


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, list) and len(value) == 0)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_json(value: Any, depth: int = 0) -> bool:
    if depth > 32:
        return False
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json(v, depth + 1) for k, v in value.items())
    return False


def _matches(pattern: str, value: str) -> bool:
    try:
        return re.search(pattern, value) is not None
    except re.error:
        return False


def _fmt(n: Any) -> str:
    return str(int(n)) if isinstance(n, float) and n.is_integer() else str(n)


def validate_cli_config_value(entry: Any, value: Any) -> Optional[str]:
    """Checks a new value against an entry's type and validation rules; the
    error message, or ``None`` when valid.  ``None`` resets the setting:
    refused only for a ``required`` entry without a default.  ``entry`` is a
    :class:`CliConfigEntry` (or anything with ``type``, ``validation`` and
    ``default``).  The rules, the same on every server:

    - ``string``, ``secret``: a string; ``min``/``max`` its length, ``pattern``, ``options``.
    - ``url``: an absolute URL (``scheme://...``); ``min``/``max`` length, ``pattern``.
    - ``number``: a finite number; ``min``/``max`` its value.
    - ``boolean``: true or false.
    - ``enum``: one of ``options``.
    - ``string[]``: a list of strings; ``min``/``max`` the item count, ``pattern`` and ``options`` each item.
    - ``duration``: ``<integer><ms|s|m|h|d>``; ``min``/``max`` in milliseconds.
    - ``json``: any JSON value.
    - ``required``: not ``''``, not ``[]``.
    """
    entry_type = getattr(entry, "type", None)
    rules = CliConfigValidation.of(getattr(entry, "validation", None)) or CliConfigValidation()
    if value is None:
        return "is required" if rules.required and _is_empty(getattr(entry, "default", None)) else None
    if rules.required and _is_empty(value):
        return "is required"

    def length_rules(text: str) -> Optional[str]:
        if rules.min is not None and len(text) < rules.min:
            return f"must be at least {_fmt(rules.min)} characters"
        if rules.max is not None and len(text) > rules.max:
            return f"must be at most {_fmt(rules.max)} characters"
        if rules.pattern is not None and not _matches(rules.pattern, text):
            return f"must match {rules.pattern}"
        return None

    if entry_type in ("string", "secret"):
        if not isinstance(value, str):
            return "must be a string"
        error = length_rules(value)
        if error:
            return error
        if rules.options and value != "" and value not in rules.options:
            return f"must be one of: {', '.join(rules.options)}"
        return None
    if entry_type == "url":
        if not isinstance(value, str):
            return "must be a string"
        if value == "":
            return None
        try:
            parts = urlsplit(value)
            valid = bool(parts.scheme) and bool(parts.netloc) and not any(c.isspace() for c in value)
            parts.port  # noqa: B018 (raises ValueError on an invalid port)
        except ValueError:
            valid = False
        if not valid or not _URL_SCHEME.match(value):
            return "must be an absolute URL"
        return length_rules(value)
    if entry_type == "number":
        if not _is_number(value):
            return "must be a number"
        if rules.min is not None and value < rules.min:
            return f"must be at least {_fmt(rules.min)}"
        if rules.max is not None and value > rules.max:
            return f"must be at most {_fmt(rules.max)}"
        return None
    if entry_type == "boolean":
        return None if isinstance(value, bool) else "must be true or false"
    if entry_type == "enum":
        if not isinstance(value, str):
            return "must be a string"
        options = rules.options or []
        return None if value in options else f"must be one of: {', '.join(options)}"
    if entry_type == "string[]":
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            return "must be a list of strings"
        if rules.min is not None and len(value) < rules.min:
            return f"must have at least {_fmt(rules.min)} items"
        if rules.max is not None and len(value) > rules.max:
            return f"must have at most {_fmt(rules.max)} items"
        for item in value:
            if rules.pattern is not None and not _matches(rules.pattern, item):
                return f"'{item}' must match {rules.pattern}"
            if rules.options and item not in rules.options:
                return f"'{item}' must be one of: {', '.join(rules.options)}"
        return None
    if entry_type == "duration":
        ms = parse_cli_config_duration(value)
        if ms is None:
            return "must be a duration such as 500ms, 30s, 15m, 12h or 7d"
        if rules.min is not None and ms < rules.min:
            return f"must be at least {format_cli_config_duration(rules.min)}"
        if rules.max is not None and ms > rules.max:
            return f"must be at most {format_cli_config_duration(rules.max)}"
        return None
    if entry_type == "json":
        return None if _is_json(value) else "must be a JSON value"
    return f"unknown type {entry_type}"


_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


def parse_cli_config_input(entry_type: str, text: str) -> CliConfigValue:
    """A value typed in the terminal (``config set cors.cors.allowedOrigins a,b``)
    as the entry's type: ``number`` parsed, ``boolean`` from
    true/false/yes/no/on/off/1/0, ``string[]`` from a JSON array or a
    comma-separated list, ``json`` parsed, anything else as typed.  Raises
    :class:`ValueError` with the reason when the text does not parse."""
    trimmed = text.strip()
    if entry_type == "number":
        try:
            n = float(trimmed)
        except ValueError:
            n = math.nan
        if trimmed == "" or not math.isfinite(n):
            raise ValueError("must be a number")
        return int(n) if n.is_integer() and re.fullmatch(r"[+-]?\d+", trimmed) else n
    if entry_type == "boolean":
        lower = trimmed.lower()
        if lower in _TRUE:
            return True
        if lower in _FALSE:
            return False
        raise ValueError("must be true or false")
    if entry_type == "string[]":
        if trimmed.startswith("["):
            try:
                parsed = json.loads(trimmed)
            except ValueError:
                raise ValueError("must be a JSON array or a comma-separated list") from None
            if not isinstance(parsed, list):
                raise ValueError("must be a JSON array or a comma-separated list")
            return parsed
        return [] if trimmed == "" else [s.strip() for s in trimmed.split(",") if s.strip()]
    if entry_type == "json":
        try:
            return json.loads(trimmed)
        except ValueError:
            raise ValueError("must be JSON") from None
    return text


def infer_cli_config_entry_type(value: Any) -> str:
    """The entry type of a value of a free key (a ``custom`` section): number, boolean, string[], json or string."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "string[]" if all(isinstance(v, str) for v in value) else "json"
    if isinstance(value, dict):
        return "json"
    return "string"


def clone_cli_config_value(value: Any) -> Any:
    """A copy of a JSON value (so callers never share lists or dicts with a store)."""
    if isinstance(value, (list, dict)):
        return copy.deepcopy(value)
    return value


def cli_config_values_equal(a: Any, b: Any) -> bool:
    """True when two JSON values are equal."""
    if a is b:
        return True
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return bool(a == b)


__all__ = [
    "parse_cli_config_duration",
    "format_cli_config_duration",
    "validate_cli_config_value",
    "parse_cli_config_input",
    "infer_cli_config_entry_type",
    "clone_cli_config_value",
    "cli_config_values_equal",
]
