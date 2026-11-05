"""Name and icon of the admin dashboard, served with its color scheme.

An application ships the dashboard under its own name and icon (browser tab,
sidebar, sign-in page) and names its terminal (the CLI's welcome banner).
Anything left out keeps the Qodalis default (Qodalis Cli Admin, the Qodalis
Solutions logo, Web CLI).
"""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlparse

_MAX_NAME = 80
_MAX_ICON = 200_000
_DATA_IMAGE = re.compile(
    r"^data:image/(?:png|jpeg|gif|webp|svg\+xml|x-icon|vnd\.microsoft\.icon);base64,[A-Za-z0-9+/]+={0,2}$"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _text(value: Any, prop: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f'Admin branding "{prop}" must be a string')
    text = value.strip()
    if not text:
        return None
    if len(text) > _MAX_NAME:
        raise ValueError(f'Admin branding "{prop}" is longer than {_MAX_NAME} characters')
    if _CONTROL.search(text):
        raise ValueError(f'Admin branding "{prop}" may not contain control characters')
    return text


def is_branding_icon(value: Any) -> bool:
    """True for an absolute http(s) URL, a root-relative path or a base64 ``data:image`` URL."""
    if not isinstance(value, str) or not value or len(value) > _MAX_ICON:
        return False
    if _DATA_IMAGE.match(value):
        return True
    if value.startswith("/"):
        return not re.match(r"^/[/\\]", value) and not re.search(r"[\s\x00-\x1f\x7f]", value)
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def validate_admin_branding(branding: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Validate the branding and return its ``theme.json`` part (``name``, ``icon``, ``cli.name``).

    Keys: ``name``, ``icon_url`` (or ``iconUrl``), ``cli_name`` (or ``cliName``).
    Returns ``None`` when nothing is set; raises :class:`ValueError` on a bad value.
    """
    if not branding:
        return None
    doc: dict[str, Any] = {}
    name = _text(branding.get("name"), "name")
    if name:
        doc["name"] = name
    icon = branding.get("icon_url", branding.get("iconUrl"))
    if isinstance(icon, str):
        icon = icon.strip()
    if icon:
        if not is_branding_icon(icon):
            raise ValueError(
                'Admin branding "icon_url" must be an absolute http(s) URL, a root-relative path '
                "or a base64 data:image URL"
            )
        doc["icon"] = icon
    cli_name = _text(branding.get("cli_name", branding.get("cliName")), "cli_name")
    if cli_name:
        doc["cli"] = {"name": cli_name}
    return doc or None
