"""Admin dashboard controllers."""

from .status_controller import create_status_router
from .plugins_controller import create_plugins_router
from .config_controller import create_config_router
from .logs_controller import create_logs_router
from .ws_clients_controller import create_ws_clients_router

__all__ = [
    "create_config_router",
    "create_logs_router",
    "create_plugins_router",
    "create_status_router",
    "create_ws_clients_router",
]
