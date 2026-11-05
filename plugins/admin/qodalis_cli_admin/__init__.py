"""Qodalis CLI admin plugin — admin dashboard for CLI servers."""

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
from .services.log_ring_buffer import LogRingBuffer
from .services.module_registry import ModuleRegistry

__all__ = [
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
