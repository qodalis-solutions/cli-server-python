"""The admin dashboard's Migrations page (``@qodalis/cli-dashboard-module-migrations``), served at
``/qcli/migrations``, outside the API, so the dashboard's ``import()`` needs no token."""

from __future__ import annotations

import hashlib
import os
from typing import Any

from starlette.staticfiles import StaticFiles

from .migrations import MIGRATIONS_READ

MIGRATIONS_DASHBOARD_PREFIX = "/qcli/migrations"

_TITLES = {
    "es": "Migraciones", "fr": "Migrations", "de": "Migrationen", "pt": "Migrações", "it": "Migrazioni",
    "ja": "マイグレーション", "ko": "마이그레이션", "zh": "迁移", "ru": "Миграции", "ro": "Migrări",
}


def find_migrations_page(directory: str | None = None) -> str | None:
    """The page's build: ``directory``, else the copy inside the package (``scripts/copy_dashboard.py``)."""
    candidate = directory or os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")
    return candidate if os.path.isfile(os.path.join(candidate, "extension.js")) else None


class _NoCacheStaticFiles(StaticFiles):
    def file_response(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        # The URLs carry the content's hash (?v=), so a new build is fetched at once
        response.headers["Cache-Control"] = "no-cache"
        return response


def migrations_page(directory: str) -> tuple[StaticFiles, dict[str, Any]]:
    """The static files app and the dashboard extension of the page in ``directory``."""

    def version(name: str) -> str | None:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]

    css = version("extension.css")
    extension: dict[str, Any] = {
        "id": "migrations",
        "title": "Migrations",
        "titles": dict(_TITLES),
        "icon": "🗄️",
        "group": "data",
        "order": 20,
        "permission": MIGRATIONS_READ,
        "entry": f"{MIGRATIONS_DASHBOARD_PREFIX}/extension.js?v={version('extension.js')}",
    }
    if css:
        extension["styles"] = [f"{MIGRATIONS_DASHBOARD_PREFIX}/extension.css?v={css}"]
    return _NoCacheStaticFiles(directory=directory), extension
