"""Color scheme of the admin dashboard.

The dashboard reads it from ``GET /qcli/admin/theme.json`` before its first
render; every token left out keeps the dashboard's default (see the README for
the list and the defaults).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

#: Color tokens of the dashboard, the same names on every server.
ADMIN_THEME_TOKENS: tuple[str, ...] = (
    "primary",
    "primaryHover",
    "onPrimary",
    "accent",
    "background",
    "backgroundAccent",
    "sidebar",
    "surface",
    "popover",
    "control",
    "controlHover",
    "border",
    "overlay",
    "heading",
    "text",
    "muted",
    "subtle",
    "success",
    "warning",
    "error",
    "info",
)

ADMIN_THEME_MODES: tuple[str, ...] = ("dark", "light", "system")

_MAX_COLOR_LENGTH = 100

# A hex color, a color function with plain numeric arguments, or a named color:
# nothing that can end the declaration (; { } quotes) or load anything (url(, var().
_COLOR_PATTERNS = (
    re.compile(r"^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$", re.IGNORECASE),
    re.compile(r"^(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\([0-9a-z.,%+\-\s/]*\)$", re.IGNORECASE),
    re.compile(r"^[a-z]{3,30}$", re.IGNORECASE),
)

_SNAKE_TO_TOKEN = {
    re.sub(r"[A-Z]", lambda m: "_" + m.group(0).lower(), t): t for t in ADMIN_THEME_TOKENS
}


def is_theme_color(value: Any) -> bool:
    """Return *True* when *value* is a color the dashboard accepts."""
    if not isinstance(value, str):
        return False
    v = value.strip()
    return 0 < len(v) <= _MAX_COLOR_LENGTH and any(p.fullmatch(v) for p in _COLOR_PATTERNS)


def _validate_colors(colors: Any, scheme: str) -> dict[str, str]:
    if colors is None:
        return {}
    if not isinstance(colors, Mapping):
        raise ValueError(f'Invalid admin theme: "{scheme}" must be a mapping of colors')
    result: dict[str, str] = {}
    for key, value in colors.items():
        # camelCase as on the wire, or snake_case (primary_hover)
        token = key if key in ADMIN_THEME_TOKENS else _SNAKE_TO_TOKEN.get(key)
        if token is None:
            raise ValueError(
                f'Invalid admin theme: unknown token "{scheme}.{key}"'
                f" (expected one of {', '.join(ADMIN_THEME_TOKENS)})"
            )
        if value is None:
            continue
        if not is_theme_color(value):
            raise ValueError(
                f'Invalid admin theme color for "{scheme}.{token}": {value!r}'
                " (use a hex, rgb()/hsl()/oklch() or named color)"
            )
        result[token] = value.strip()
    return result


def validate_admin_theme(theme: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate a theme and return the document served as ``theme.json``.

    *theme* is ``{"mode": "dark" | "light" | "system", "dark": {...},
    "light": {...}}``; token keys are camelCase (``primaryHover``) or
    snake_case (``primary_hover``). Raises :class:`ValueError` on an unknown
    mode or token, or a value that is not a plain color.
    """
    if theme is None:
        theme = {}
    if not isinstance(theme, Mapping):
        raise ValueError("Invalid admin theme: expected a mapping")
    unknown = set(theme) - {"mode", "dark", "light"}
    if unknown:
        raise ValueError(f"Invalid admin theme: unknown keys {sorted(unknown)} (expected mode, dark, light)")
    mode = theme.get("mode") or "dark"
    if mode not in ADMIN_THEME_MODES:
        raise ValueError(f"Invalid admin theme mode {mode!r} (expected dark, light or system)")
    return {
        "mode": mode,
        "dark": _validate_colors(theme.get("dark"), "dark"),
        "light": _validate_colors(theme.get("light"), "light"),
    }


def admin_theme_json(theme: Mapping[str, Any] | None) -> bytes:
    """The validated theme as the JSON body of ``theme.json``."""
    return json.dumps(validate_admin_theme(theme), separators=(",", ":")).encode()
