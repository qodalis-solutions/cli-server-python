"""Users plugin middleware."""

from .auth_middleware import create_auth_dependencies

__all__ = ["create_auth_dependencies"]
