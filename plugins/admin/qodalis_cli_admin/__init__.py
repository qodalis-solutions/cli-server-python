"""Qodalis CLI admin plugin — admin dashboard for CLI servers."""

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🎛️"

from .cli_admin_builder import CliAdminBuilder, CliAdminPlugin, AdminBuildDeps, BroadcastFn
from .auth.jwt_service import JwtService
from .auth.auth_middleware import create_auth_dependency
from .auth.auth_controller import create_auth_router
from .dashboard_resolver import resolve_dashboard_dir
from .services.admin_config import AdminConfig
from .services.admin_branding import is_branding_icon, validate_admin_branding
from .services.admin_theme import (
    ADMIN_THEME_MODES,
    ADMIN_THEME_TOKENS,
    admin_theme_json,
    is_theme_color,
    validate_admin_theme,
)
from .commands.admin_command import (
    ADMIN_MODULE_NAME,
    LOGS_READ_PERMISSION,
    AdminCommandDeps,
    AdminModule,
    CliAdminCommandProcessor,
    format_duration,
    format_log_line,
    format_memory_line,
    format_memory_size,
    format_table,
)
from .controllers.status_controller import status_snapshot
from .services.status_history import summarize_status_history
from .services.system_memory import SystemMemoryReaders, read_system_memory
from .services.time_range import parse_since
from .services.log_ring_buffer import LogRingBuffer
from .services.module_registry import ModuleRegistry

__all__ = [
    "ADMIN_MODULE_NAME",
    "LOGS_READ_PERMISSION",
    "AdminCommandDeps",
    "AdminModule",
    "CliAdminCommandProcessor",
    "format_duration",
    "format_log_line",
    "format_memory_line",
    "format_memory_size",
    "format_table",
    "read_system_memory",
    "SystemMemoryReaders",
    "status_snapshot",
    "summarize_status_history",
    "parse_since",
    "ADMIN_THEME_MODES",
    "ADMIN_THEME_TOKENS",
    "AdminBuildDeps",
    "AdminConfig",
    "BroadcastFn",
    "CliAdminBuilder",
    "CliAdminPlugin",
    "JwtService",
    "LogRingBuffer",
    "ModuleRegistry",
    "admin_theme_json",
    "create_auth_dependency",
    "create_auth_router",
    "is_branding_icon",
    "is_theme_color",
    "resolve_dashboard_dir",
    "validate_admin_branding",
    "validate_admin_theme",
]
