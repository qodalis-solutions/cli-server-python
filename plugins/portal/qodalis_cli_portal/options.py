"""Validation of the portal's options (contract C8), with the admin plugin's
rules for names, logos and colours (the colour patterns are those of
``qodalis_cli_admin.services.admin_theme``)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

#: The dashboard's colour tokens (``theme.json``), the same on every server.
THEME_TOKENS: tuple[str, ...] = (
    "primary", "primaryHover", "onPrimary", "accent", "background", "backgroundAccent", "sidebar",
    "surface", "popover", "control", "controlHover", "border", "overlay", "heading", "text", "muted",
    "subtle", "success", "warning", "error", "info",
)
THEME_MODES: tuple[str, ...] = ("dark", "light", "system")
MAX_NAME = 80
MAX_LOGO = 200_000
MAX_FOOTER_LINKS = 20
MAX_COLOR_LENGTH = 100

NAME_TEXT = "portal name must be 1-80 characters without control characters"
LOGO_TEXT = "portal logo must be an http(s) URL, a root-relative path or a data:image URL"

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_DATA_IMAGE = re.compile(
    r"^data:image/(?:png|jpeg|gif|webp|svg\+xml|x-icon|vnd\.microsoft\.icon);base64,[A-Za-z0-9+/]+={0,2}$"
)
# A hex colour, a colour function with plain numeric arguments, or a named
# colour: nothing that can end the declaration (; { } quotes) or load anything.
_COLOR_PATTERNS = (
    re.compile(r"^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$", re.IGNORECASE),
    re.compile(r"^(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\([0-9a-z.,%+\-\s/]*\)$", re.IGNORECASE),
    re.compile(r"^[a-z]{3,30}$", re.IGNORECASE),
)
_SNAKE_TO_TOKEN = {re.sub(r"[A-Z]", lambda m: "_" + m.group(0).lower(), t): t for t in THEME_TOKENS}


def validate_path(path: Any) -> str:
    """The mount: starts with ``/``, no trailing ``/`` except ``/`` itself;
    nothing that could leave a URL or an HTML attribute (``//``, ``\\``,
    whitespace, ``?``, ``#``, quotes, ``<``, ``>``)."""
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or (path != "/" and path.endswith("/"))
        or "//" in path
        or any(c.isspace() or ord(c) < 0x20 or c in "?#\\\"'<>" for c in path)
    ):
        raise ValueError(f"invalid portal path: {path}")
    return path


def validate_name(name: Any) -> str:
    if not isinstance(name, str) or not 1 <= len(name) <= MAX_NAME or _CONTROL.search(name):
        raise ValueError(NAME_TEXT)
    return name


def _is_root_relative(value: str) -> bool:
    return (
        value.startswith("/")
        and not re.match(r"^/[/\\]", value)
        and not re.search(r"[\s\x00-\x1f\x7f]", value)
    )


def _is_http_url(value: str) -> bool:
    if re.search(r"[\s\x00-\x1f\x7f]", value):
        return False
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def validate_logo(logo: Any) -> str | None:
    if logo is None:
        return None
    if (
        not isinstance(logo, str)
        or not logo
        or len(logo) > MAX_LOGO
        or not (_DATA_IMAGE.match(logo) or _is_root_relative(logo) or _is_http_url(logo))
    ):
        raise ValueError(LOGO_TEXT)
    return logo


def is_color(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    v = value.strip()
    return 0 < len(v) <= MAX_COLOR_LENGTH and any(p.fullmatch(v) for p in _COLOR_PATTERNS)


def _colors(colors: Any, scheme: str) -> dict[str, str]:
    if colors is None:
        return {}
    if not isinstance(colors, Mapping):
        raise ValueError(f"invalid portal colour {scheme}: {colors}")
    result: dict[str, str] = {}
    for key, value in colors.items():
        token = key if key in THEME_TOKENS else _SNAKE_TO_TOKEN.get(key)
        if value is None and token is not None:
            continue
        if token is None or not is_color(value):
            raise ValueError(f"invalid portal colour {scheme}.{key}: {value}")
        result[token] = value.strip()
    return result


def validate_theme(theme: Mapping[str, Any] | None) -> dict[str, Any]:
    """``{"mode", "dark", "light"}`` with only the set colours; the mode
    defaults to ``system``."""
    theme = theme or {}
    if not isinstance(theme, Mapping):
        raise ValueError(f"invalid portal theme mode: {theme}")
    mode = theme.get("mode") or "system"
    if mode not in THEME_MODES:
        raise ValueError(f"invalid portal theme mode: {mode}")
    return {"mode": mode, "dark": _colors(theme.get("dark"), "dark"), "light": _colors(theme.get("light"), "light")}


def validate_footer_links(links: list[tuple[Any, Any]]) -> list[dict[str, str]]:
    """At most 20 links; ``label`` 1-80 characters, ``url`` http(s) or
    root-relative.  The error names the offending (or the 21st) link."""
    result: list[dict[str, str]] = []
    for index, (label, url) in enumerate(links):
        valid = (
            index < MAX_FOOTER_LINKS
            and isinstance(label, str)
            and 1 <= len(label) <= MAX_NAME
            and not _CONTROL.search(label)
            and isinstance(url, str)
            and (_is_root_relative(url) or _is_http_url(url))
        )
        if not valid:
            raise ValueError(f"invalid portal footer link: {label}")
        result.append({"label": label, "url": url})
    return result
