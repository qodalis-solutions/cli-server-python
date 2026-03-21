from __future__ import annotations


class AwsConfigService:
    """Manages AWS credentials, region, and profile configuration in memory."""

    def __init__(self) -> None:
        self._access_key_id: str | None = None
        self._secret_access_key: str | None = None
        self._region: str | None = None
        self._profile: str | None = None

    # -- getters -------------------------------------------------------------

    def get_access_key_id(self) -> str | None:
        return self._access_key_id

    def get_secret_access_key(self) -> str | None:
        return self._secret_access_key

    def get_region(self) -> str | None:
        return self._region

    def get_profile(self) -> str | None:
        return self._profile

    # -- setters -------------------------------------------------------------

    def set_credentials(self, access_key_id: str, secret_access_key: str) -> None:
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key

    def set_region(self, region: str) -> None:
        self._region = region

    def set_profile(self, profile: str) -> None:
        self._profile = profile

    # -- summary -------------------------------------------------------------

    def get_config_summary(self) -> dict[str, str | None]:
        return {
            "access_key_id": self._mask_key(self._access_key_id) if self._access_key_id else None,
            "secret_access_key": "****" if self._secret_access_key else None,
            "region": self._region,
            "profile": self._profile,
        }

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _mask_key(key: str) -> str:
        if len(key) <= 8:
            return "****"
        return key[:4] + "***" + key[-5:]
