"""The server plugin packages a host installed (admin dashboard's plugins page)."""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from importlib import metadata
from typing import Any

CORE_DISTRIBUTION = "qodalis-cli-server"
PLUGIN_PREFIX = "qodalis-cli-server-plugin-"

# Top-level modules whose distribution does not follow the plugin convention.
_KNOWN_DISTRIBUTIONS = {
    "qodalis_cli": CORE_DISTRIBUTION,
    "qodalis_cli_server_abstractions": "qodalis-cli-server-abstractions",
}


@dataclass(frozen=True)
class CliInstalledPlugin:
    """A server plugin package the host installed (users, admin, jobs, a
    storage provider, ...), read from its distribution metadata."""

    #: The distribution name, e.g. ``qodalis-cli-server-plugin-users``.
    id: str
    #: A short name, e.g. ``Users`` or ``Data Explorer Sql``; ``Core`` for the server.
    name: str
    version: str | None = None
    description: str | None = None
    #: One emoji or an image URL; ``None`` when the package has none.
    icon: str | None = None
    #: ``marketplace`` for a server plugin installed at runtime, ``code`` for one the host added through its entry
    #: (``builder.add_plugin``); ``None`` for the host's own.
    source: str | None = None
    #: ``needs-configuration`` when its required settings have no value (it registered nothing).
    status: str | None = None
    #: Those settings (``section.key``) or the reason.
    missing: tuple[str, ...] | None = None
    #: The id of its settings provider (``config get <id>``).
    settings: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "icon": self.icon,
        }
        if self.source:
            out["source"] = self.source
        if self.status:
            out["status"] = self.status
        if self.missing:
            out["missing"] = list(self.missing)
        if self.settings:
            out["settings"] = self.settings
        return out

    @classmethod
    def from_distribution(
        cls,
        distribution: str,
        *,
        name: str | None = None,
        version: str | None = None,
        description: str | None = None,
        icon: str | None = None,
    ) -> CliInstalledPlugin:
        """Describes an installed distribution.  Version and description come
        from its metadata; a distribution that is not installed (running from
        source) keeps the given values or ``None``.  The icon is the
        ``__plugin_icon__`` of the distribution's top-level package (see
        :func:`plugin_icon`)."""
        if version is None or description is None:
            try:
                meta = metadata.metadata(distribution)
                version = version or meta.get("Version")
                description = description or meta.get("Summary")
            except metadata.PackageNotFoundError:
                pass
        return cls(
            id=distribution,
            name=name or short_plugin_name(distribution),
            version=version,
            description=description,
            icon=icon or plugin_icon(distribution),
        )


def _normalize(distribution: str) -> str:
    return distribution.lower().replace("_", "-").replace(".", "-")


def _top_level_packages(distribution: str) -> list[str]:
    """The import names a distribution ships: from the installed metadata,
    else by the naming convention (``qodalis-cli-server-plugin-users`` ->
    ``qodalis_cli_users``)."""
    wanted = _normalize(distribution)
    try:
        tops = [top for top, dists in metadata.packages_distributions().items()
                if any(_normalize(d) == wanted for d in dists)]
    except Exception:  # pragma: no cover - broken metadata
        tops = []
    for top, dist in _KNOWN_DISTRIBUTIONS.items():
        if dist == distribution:
            tops.append(top)
    if distribution.startswith(PLUGIN_PREFIX):
        tops.append("qodalis_cli_" + distribution[len(PLUGIN_PREFIX):].replace("-", "_"))
    return list(dict.fromkeys(tops))


def plugin_icon(distribution: str) -> str | None:
    """The icon a distribution declares: ``__plugin_icon__ = "💳"`` (one
    emoji or an image URL) in its top-level package's ``__init__.py``."""
    for top in _top_level_packages(distribution):
        module = sys.modules.get(top)
        if module is None:
            try:
                module = importlib.import_module(top)
            except Exception:
                continue
        icon = getattr(module, "__plugin_icon__", None)
        if isinstance(icon, str) and icon.strip():
            return icon.strip()
    return None


def short_plugin_name(distribution: str) -> str:
    """``qodalis-cli-server-plugin-data-explorer-sql`` -> ``Data Explorer Sql``,
    ``qodalis-cli-server`` -> ``Core``; other names are kept as they are."""
    if distribution == CORE_DISTRIBUTION:
        return "Core"
    if distribution.startswith(PLUGIN_PREFIX):
        words = distribution[len(PLUGIN_PREFIX):].replace("_", "-").split("-")
        return " ".join(w.capitalize() for w in words if w)
    return distribution


def distribution_of(obj: Any) -> str | None:
    """The distribution that ships ``obj`` (a module, class or instance), or
    ``None`` for the host application's own code."""
    module_name = obj.__name__ if isinstance(obj, type(sys)) else (
        obj.__module__ if isinstance(obj, type) else type(obj).__module__
    )
    top = (module_name or "").split(".")[0]
    if not top or top == "__main__":
        return None
    try:
        dists = metadata.packages_distributions().get(top)
    except Exception:  # pragma: no cover - broken metadata
        dists = None
    if dists:
        return dists[0]
    if top in _KNOWN_DISTRIBUTIONS:
        return _KNOWN_DISTRIBUTIONS[top]
    if top.startswith("qodalis_cli_"):
        return PLUGIN_PREFIX + top[len("qodalis_cli_"):].replace("_", "-")
    return None
