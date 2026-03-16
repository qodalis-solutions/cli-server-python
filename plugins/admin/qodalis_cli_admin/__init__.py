"""Qodalis CLI admin plugin — admin dashboard for CLI servers."""

from .cli_admin_builder import CliAdminBuilder, CliAdminPlugin, AdminBuildDeps, BroadcastFn
from .auth.jwt_service import JwtService
from .auth.auth_middleware import create_auth_dependency
from .auth.auth_controller import create_auth_router
from .dashboard_resolver import resolve_dashboard_dir
from .services.admin_config import AdminConfig
from .services.log_ring_buffer import LogRingBuffer
from .services.module_registry import ModuleRegistry

__all__ = [
    "AdminBuildDeps",
    "AdminConfig",
    "BroadcastFn",
    "CliAdminBuilder",
    "CliAdminPlugin",
    "JwtService",
    "LogRingBuffer",
    "ModuleRegistry",
    "create_auth_dependency",
    "create_auth_router",
    "resolve_dashboard_dir",
]
