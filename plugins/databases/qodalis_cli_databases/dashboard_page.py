"""The admin dashboard's Databases page (``@qodalis/cli-dashboard-module-databases``), served at
``/qcli/databases``, outside the API, so the dashboard's ``import()`` needs no token; it also puts cards on the
dashboard's Overview."""

from __future__ import annotations

import os
from typing import Optional

from qodalis_cli_server_abstractions.dashboard_modules import DashboardModulePage, module_page_of

from .databases import DATABASES_READ

DATABASES_DASHBOARD_PREFIX = "/qcli/databases"

#: The page's registration (entry and styles are added when served).
DATABASES_DASHBOARD_PAGE = {
    "id": "databases",
    "title": "Databases",
    "titles": {
        "es": "Bases de datos", "fr": "Bases de données", "de": "Datenbanken", "pt": "Bancos de dados",
        "it": "Database", "ja": "データベース", "ko": "데이터베이스", "zh": "数据库", "ru": "Базы данных",
        "ro": "Baze de date",
    },
    "icon": "🗄️",
    "group": "data",
    "order": 20,
    "permission": DATABASES_READ,
    "widgets": ["overview"],
}


def databases_page(directory: str | bool | None = None) -> Optional[DashboardModulePage]:
    """The page: ``directory`` another build, ``False`` none, default the copy in the package
    (``scripts/copy_dashboard.py``); ``None`` when there is no build."""
    return module_page_of(
        os.path.dirname(os.path.abspath(__file__)), directory, DATABASES_DASHBOARD_PREFIX, DATABASES_DASHBOARD_PAGE,
    )
