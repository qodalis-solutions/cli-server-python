"""The admin dashboard's Databases page (``@qodalis/cli-dashboard-module-databases``), served at
``/qcli/databases``, outside the API, so the dashboard's ``import()`` needs no token."""

from __future__ import annotations

import hashlib
import os
from typing import Any

from starlette.staticfiles import StaticFiles

from .databases import DATABASES_READ

DATABASES_DASHBOARD_PREFIX = "/qcli/databases"

_TITLES = {
    "es": "Bases de datos", "fr": "Bases de données", "de": "Datenbanken", "pt": "Bancos de dados", "it": "Database",
    "ja": "データベース", "ko": "데이터베이스", "zh": "数据库", "ru": "Базы данных", "ro": "Baze de date",
}


def find_databases_page(directory: str | None = None) -> str | None:
    """The page's build: ``directory``, else the copy inside the package (``scripts/copy_dashboard.py``)."""
    candidate = directory or os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")
    return candidate if os.path.isfile(os.path.join(candidate, "extension.js")) else None


class _NoCacheStaticFiles(StaticFiles):
    def file_response(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        # The URLs carry the content's hash (?v=), so a new build is fetched at once
        response.headers["Cache-Control"] = "no-cache"
        return response


def databases_page(directory: str) -> tuple[StaticFiles, dict[str, Any]]:
    """The static files app and the dashboard extension of the page in ``directory``."""

    def version(name: str) -> str | None:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]

    css = version("extension.css")
    extension: dict[str, Any] = {
        "id": "databases",
        "title": "Databases",
        "titles": dict(_TITLES),
        "icon": "🗄️",
        "group": "data",
        "order": 20,
        "permission": DATABASES_READ,
        "entry": f"{DATABASES_DASHBOARD_PREFIX}/extension.js?v={version('extension.js')}",
    }
    if css:
        extension["styles"] = [f"{DATABASES_DASHBOARD_PREFIX}/extension.css?v={css}"]
    return _NoCacheStaticFiles(directory=directory), extension
