"""Pages plugins add to the admin dashboard (the dashboard's ``docs/extensions.md``)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping

#: The navigation groups; any other value becomes ``system``.
DASHBOARD_EXTENSION_GROUPS = ("overview", "operations", "data", "access", "system")

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_WHITESPACE_OR_CONTROL = re.compile(r"[\s\x00-\x1f\x7f]")
_NOT_IN_EMOJI = re.compile(r"[\s<>\"'/\\]")
_HTTP_URL = re.compile(r"^https?://[^/]", re.IGNORECASE)


def _invalid(reason: str) -> ValueError:
    return ValueError(f"invalid dashboard extension: {reason}")


def _is_url(value: Any) -> bool:
    """A root-relative path (not ``//``) or an http(s) URL, without whitespace or control characters."""
    if not isinstance(value, str) or not value or len(value) > 2048 or _WHITESPACE_OR_CONTROL.search(value):
        return False
    if value.startswith("/"):
        return not value.startswith("//") and not value.startswith("/\\")
    return bool(_HTTP_URL.match(value))


def _is_single_grapheme(value: str) -> bool:
    """One user-perceived character: a base code point followed only by combining marks, variation
    selectors, zero-width joiners with their following code point, skin tones, or (for a flag) a
    second regional indicator."""
    chars = list(value)
    if not chars:
        return False
    i = 1
    regional = 0x1F1E6 <= ord(chars[0]) <= 0x1F1FF
    while i < len(chars):
        cp = ord(chars[i])
        if cp == 0x200D and i + 1 < len(chars):  # zero-width joiner + the next code point
            i += 2
            continue
        if regional and i == 1 and 0x1F1E6 <= cp <= 0x1F1FF:
            i += 1
            continue
        if 0xFE00 <= cp <= 0xFE0F or 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F or unicodedata.combining(chars[i]) or cp == 0x20E3:
            i += 1
            continue
        return False
    return True


def _is_icon(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if _is_url(value):
        return True
    if len(value) > 32 or _NOT_IN_EMOJI.search(value):
        return False
    return _is_single_grapheme(value)


@dataclass(frozen=True)
class CliDashboardExtension:
    """A page a plugin adds to the admin dashboard: the dashboard lists it in its
    navigation and, when it is opened, loads ``entry`` (an ES module the plugin
    serves) and mounts it.  Registered with ``builder.add_dashboard_extension(...)``
    or a mounted plugin's ``dashboard_extensions``; served by the admin plugin at
    ``GET /api/v1/qcli/dashboard/extensions``."""

    #: ``^[a-z0-9][a-z0-9-]{0,39}$``, unique; also the page's path segment.
    id: str
    #: 1-40 characters.
    title: str
    #: A root-relative path (``/...``, not ``//``) or an http(s) URL.
    entry: str
    #: Titles by language code (``de``, ``pt-BR``).
    titles: Mapping[str, str] | None = None
    #: One emoji, or an http(s) or root-relative image URL.
    icon: str | None = None
    #: ``overview``, ``operations``, ``data``, ``access``, ``system`` (default).
    group: str | None = None
    #: Order within the group (default 100).
    order: int | None = None
    #: The page shows only for users holding this permission (wildcards match).
    permission: str | None = None
    #: Stylesheets (same rules as ``entry``), at most 5.
    styles: tuple[str, ...] | None = field(default=None)
    #: Shown in the page's error panel.
    version: str | None = None

    @classmethod
    def of(cls, value: CliDashboardExtension | Mapping[str, Any]) -> CliDashboardExtension:
        """An extension from itself or a mapping with the JSON field names."""
        if isinstance(value, CliDashboardExtension):
            return value
        if not isinstance(value, Mapping):
            raise _invalid("not an extension or a mapping")
        styles = value.get("styles")
        return cls(
            id=value.get("id"),  # type: ignore[arg-type]
            title=value.get("title"),  # type: ignore[arg-type]
            entry=value.get("entry"),  # type: ignore[arg-type]
            titles=value.get("titles"),
            icon=value.get("icon"),
            group=value.get("group"),
            order=value.get("order"),
            permission=value.get("permission"),
            styles=tuple(styles) if isinstance(styles, (list, tuple)) else styles,
            version=value.get("version"),
        )

    def validated(self) -> CliDashboardExtension:
        """A checked copy (an unknown group becomes ``system``).  Raises
        ``ValueError`` ``invalid dashboard extension: <reason>``."""
        ext_id = self.id
        if not isinstance(ext_id, str) or not _ID.match(ext_id):
            raise _invalid(f"id must match {_ID.pattern}")
        if not isinstance(self.title, str) or not self.title.strip() or len(self.title) > 40:
            raise _invalid(f"{ext_id}: title must be 1-40 characters")
        if not _is_url(self.entry):
            raise _invalid(f"{ext_id}: entry must be a root-relative path or an http(s) URL")
        titles = None
        if self.titles is not None:
            if not isinstance(self.titles, Mapping) or not all(
                isinstance(k, str) and _LANGUAGE.match(k) and isinstance(v, str) and v.strip() and len(v) <= 40
                for k, v in self.titles.items()
            ):
                raise _invalid(f"{ext_id}: titles must map language codes to titles of 1-40 characters")
            titles = dict(self.titles)
        if self.icon is not None and not _is_icon(self.icon):
            raise _invalid(f"{ext_id}: icon must be an emoji or an image URL")
        group = self.group
        if group is not None:
            if not isinstance(group, str):
                raise _invalid(f"{ext_id}: group must be a string")
            group = group if group in DASHBOARD_EXTENSION_GROUPS else "system"
        if self.order is not None and (isinstance(self.order, bool) or not isinstance(self.order, int)):
            raise _invalid(f"{ext_id}: order must be an integer")
        if self.permission is not None and (
            not isinstance(self.permission, str) or not self.permission.strip() or len(self.permission) > 100
        ):
            raise _invalid(f"{ext_id}: permission must be a permission name")
        styles = None
        if self.styles is not None:
            if not isinstance(self.styles, (list, tuple)) or len(self.styles) > 5 or not all(_is_url(s) for s in self.styles):
                raise _invalid(f"{ext_id}: styles must be at most 5 root-relative paths or http(s) URLs")
            styles = tuple(self.styles)
        if self.version is not None and (not isinstance(self.version, str) or not 1 <= len(self.version) <= 50):
            raise _invalid(f"{ext_id}: version must be 1-50 characters")
        return CliDashboardExtension(
            id=ext_id,
            title=self.title,
            entry=self.entry,
            titles=titles,
            icon=self.icon,
            group=group,
            order=self.order,
            permission=self.permission,
            styles=styles,
            version=self.version,
        )

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape: the set fields only, in the contract's order."""
        out: dict[str, Any] = {"id": self.id, "title": self.title}
        if self.titles is not None:
            out["titles"] = dict(self.titles)
        for key in ("icon", "group", "order", "permission"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        out["entry"] = self.entry
        if self.styles is not None:
            out["styles"] = list(self.styles)
        if self.version is not None:
            out["version"] = self.version
        return out
