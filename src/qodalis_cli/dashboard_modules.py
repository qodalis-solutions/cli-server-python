"""Dashboard module pages (see :mod:`qodalis_cli_server_abstractions.dashboard_modules`), and the two the core
serves itself: Filesystem (with the filesystem API) and Data explorer (with the data explorer). Their builds are
embedded in the packages ``qodalis-cli-server-plugin-filesystem`` and ``qodalis-cli-server-plugin-data-explorer``
(``scripts/copy_dashboard.py``)."""

from __future__ import annotations

import importlib
import os
from typing import Optional

from qodalis_cli_server_abstractions.dashboard_modules import (
    DASHBOARD_MODULES_PREFIX,
    DashboardModulePage,
    dashboard_module_page,
    find_dashboard_module,
    module_page_of,
)

__all__ = [
    "DASHBOARD_MODULES_PREFIX",
    "DashboardModulePage",
    "dashboard_module_page",
    "find_dashboard_module",
    "module_page_of",
    "FILESYSTEM_PAGE",
    "DATA_EXPLORER_PAGE",
    "filesystem_page",
    "data_explorer_page",
]

#: The Filesystem page's registration (entry and styles are added when served).
FILESYSTEM_PAGE = {
    "id": "filesystem",
    "title": "Filesystem",
    "titles": {
        "es": "Archivos", "fr": "Fichiers", "de": "Dateien", "pt": "Arquivos", "it": "File",
        "ja": "ファイル", "ko": "파일", "zh": "文件", "ru": "Файлы", "ro": "Fișiere",
    },
    "icon": "📁",
    "group": "data",
    "order": 10,
    "permission": "files:read",
}

#: The Data explorer page's registration.
DATA_EXPLORER_PAGE = {
    "id": "data-explorer",
    "title": "Data explorer",
    "titles": {
        "es": "Explorador de datos", "fr": "Explorateur de données", "de": "Daten-Explorer",
        "pt": "Explorador de dados", "it": "Esplora dati", "ja": "データエクスプローラー", "ko": "데이터 탐색기",
        "zh": "数据浏览器", "ru": "Обозреватель данных", "ro": "Explorator de date",
    },
    "icon": "⛁",
    "group": "data",
    "order": 15,
    "permission": "data:query",
}


def _package_dir(module: str) -> Optional[str]:
    try:
        return os.path.dirname(os.path.abspath(importlib.import_module(module).__file__ or ""))
    except ImportError:
        return None


def filesystem_page(directory: str | bool | None = None) -> Optional[DashboardModulePage]:
    """The Filesystem page: ``directory`` another build, ``False`` none, default the copy in its package."""
    package = _package_dir("qodalis_cli_filesystem")
    return module_page_of(package or "", directory, f"{DASHBOARD_MODULES_PREFIX}/filesystem", FILESYSTEM_PAGE)


def data_explorer_page(directory: str | bool | None = None) -> Optional[DashboardModulePage]:
    """The Data explorer page: ``directory`` another build, ``False`` none, default the copy in its package."""
    package = _package_dir("qodalis_cli_data_explorer")
    return module_page_of(package or "", directory, f"{DASHBOARD_MODULES_PREFIX}/data-explorer", DATA_EXPLORER_PAGE)
