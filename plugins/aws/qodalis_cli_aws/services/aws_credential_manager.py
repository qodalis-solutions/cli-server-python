from __future__ import annotations

import os
from typing import Any

import boto3

from .aws_config_service import AwsConfigService


class AwsCredentialManager:
    """Creates and caches boto3 clients using the stored AWS configuration."""

    def __init__(self, config: AwsConfigService) -> None:
        self._config = config
        self._client_cache: dict[str, Any] = {}

    def get_client(self, service_name: str, region: str | None = None) -> Any:
        """Returns a boto3 client for the given service, reusing cached instances.

        Credentials are resolved from the config service first, then from
        environment variables.

        Args:
            service_name: AWS service name (e.g. ``"s3"``, ``"ec2"``).
            region: Optional region override; falls back to config and env vars.

        Returns:
            A boto3 service client.
        """
        effective_region = (
            region
            or self._config.get_region()
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
        )
        profile = self._config.get_profile() or "default"
        cache_key = f"{service_name}:{effective_region or 'default'}:{profile}"

        if cache_key not in self._client_cache:
            kwargs: dict[str, Any] = {}

            access_key = self._config.get_access_key_id() or os.environ.get("AWS_ACCESS_KEY_ID")
            secret_key = self._config.get_secret_access_key() or os.environ.get("AWS_SECRET_ACCESS_KEY")

            if access_key and secret_key:
                kwargs["aws_access_key_id"] = access_key
                kwargs["aws_secret_access_key"] = secret_key

            if effective_region:
                kwargs["region_name"] = effective_region

            self._client_cache[cache_key] = boto3.client(service_name, **kwargs)

        return self._client_cache[cache_key]

    def clear_cache(self) -> None:
        """Clears all cached boto3 clients, forcing re-creation on next access."""
        self._client_cache.clear()
