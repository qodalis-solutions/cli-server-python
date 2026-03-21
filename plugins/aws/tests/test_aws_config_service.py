from __future__ import annotations

import pytest

from qodalis_cli_aws.services.aws_config_service import AwsConfigService


class TestAwsConfigService:
    def test_initial_state(self) -> None:
        svc = AwsConfigService()
        assert svc.get_access_key_id() is None
        assert svc.get_secret_access_key() is None
        assert svc.get_region() is None
        assert svc.get_profile() is None

    def test_set_credentials(self) -> None:
        svc = AwsConfigService()
        svc.set_credentials("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
        assert svc.get_access_key_id() == "AKIAIOSFODNN7EXAMPLE"
        assert svc.get_secret_access_key() == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

    def test_set_region(self) -> None:
        svc = AwsConfigService()
        svc.set_region("us-west-2")
        assert svc.get_region() == "us-west-2"

    def test_set_profile(self) -> None:
        svc = AwsConfigService()
        svc.set_profile("production")
        assert svc.get_profile() == "production"

    def test_config_summary_no_credentials(self) -> None:
        svc = AwsConfigService()
        summary = svc.get_config_summary()
        assert summary["access_key_id"] is None
        assert summary["secret_access_key"] is None
        assert summary["region"] is None
        assert summary["profile"] is None

    def test_config_summary_masks_key(self) -> None:
        svc = AwsConfigService()
        svc.set_credentials("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
        svc.set_region("eu-west-1")
        svc.set_profile("dev")

        summary = svc.get_config_summary()
        # Key should be masked: first 4 + *** + last 5
        assert summary["access_key_id"] == "AKIA***AMPLE"
        assert summary["secret_access_key"] == "****"
        assert summary["region"] == "eu-west-1"
        assert summary["profile"] == "dev"

    def test_mask_short_key(self) -> None:
        svc = AwsConfigService()
        svc.set_credentials("ABCD1234", "secret")
        summary = svc.get_config_summary()
        assert summary["access_key_id"] == "****"

    def test_mask_key_boundary(self) -> None:
        svc = AwsConfigService()
        svc.set_credentials("ABCDEFGHI", "secret")
        summary = svc.get_config_summary()
        # 9 chars: first 4 + *** + last 5 = "ABCD***EFGHI"
        assert summary["access_key_id"] == "ABCD***EFGHI"
