"""Qodalis CLI filesystem-s3 plugin — AWS S3-based storage provider.

Re-exports from ``qodalis_cli_filesystem_s3`` for backward compatibility.
"""

from __future__ import annotations

from qodalis_cli_filesystem_s3 import S3FileStorageProvider, S3ProviderOptions

__all__ = [
    "S3FileStorageProvider",
    "S3ProviderOptions",
]
