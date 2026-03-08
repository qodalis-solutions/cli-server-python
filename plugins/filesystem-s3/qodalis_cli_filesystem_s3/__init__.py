"""Qodalis CLI filesystem-s3 plugin — AWS S3-based storage provider."""

from __future__ import annotations

from .s3_provider import S3FileStorageProvider, S3ProviderOptions

__all__ = [
    "S3FileStorageProvider",
    "S3ProviderOptions",
]
