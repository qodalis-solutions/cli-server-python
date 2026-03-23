from __future__ import annotations


class AwsConfigService:
    """Manages AWS credentials, region, and profile configuration in memory."""

    def __init__(self) -> None:
        self._access_key_id: str | None = None
        self._secret_access_key: str | None = None
        self._region: str | None = None
        self._profile: str | None = None

    def get_access_key_id(self) -> str | None:
        """Returns the configured AWS access key ID, or ``None``."""
        return self._access_key_id

    def get_secret_access_key(self) -> str | None:
        """Returns the configured AWS secret access key, or ``None``."""
        return self._secret_access_key

    def get_region(self) -> str | None:
        """Returns the configured AWS region, or ``None``."""
        return self._region

    def get_profile(self) -> str | None:
        """Returns the configured AWS profile name, or ``None``."""
        return self._profile

    def set_credentials(self, access_key_id: str, secret_access_key: str) -> None:
        """Stores AWS access key credentials.

        Args:
            access_key_id: The AWS access key ID.
            secret_access_key: The AWS secret access key.
        """
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key

    def set_region(self, region: str) -> None:
        """Sets the default AWS region.

        Args:
            region: AWS region identifier (e.g. ``us-east-1``).
        """
        self._region = region

    def set_profile(self, profile: str) -> None:
        """Sets the AWS profile name to use.

        Args:
            profile: Profile name from ``~/.aws/credentials``.
        """
        self._profile = profile

    def get_config_summary(self) -> dict[str, str | None]:
        """Returns a summary of the current configuration with secrets masked.

        Returns:
            Dictionary with ``access_key_id``, ``secret_access_key``, ``region``,
            and ``profile`` keys.
        """
        return {
            "access_key_id": self._mask_key(self._access_key_id) if self._access_key_id else None,
            "secret_access_key": "****" if self._secret_access_key else None,
            "region": self._region,
            "profile": self._profile,
        }

    @staticmethod
    def _mask_key(key: str) -> str:
        """Masks the middle portion of a key string for safe display.

        Args:
            key: The key string to mask.

        Returns:
            The masked key with only the first 4 and last 5 characters visible.
        """
        if len(key) <= 8:
            return "****"
        return key[:4] + "***" + key[-5:]
