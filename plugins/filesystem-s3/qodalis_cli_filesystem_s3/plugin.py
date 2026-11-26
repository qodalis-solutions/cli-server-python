"""The S3 file storage's entry point (``qodalis_cli.plugins``): the filesystem API over an S3 (or S3-compatible)
bucket.  The bucket, region, prefix and endpoint are settings, the access keys ``secret`` settings (never returned;
without them the AWS default credential chain is used).  Read-only unless ``readOnly`` is turned off.  The bucket is
required: until it is set the plugin needs configuration.  Refused when the host already set the filesystem storage."""

from __future__ import annotations

from typing import Any

from qodalis_cli_filesystem import read_only_file_storage
from qodalis_cli_server_abstractions import CliPluginEntryInfo
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition, CliConfigValidation

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(id="filesystem-s3", package="qodalis-cli-server-plugin-filesystem-s3", title="Filesystem S3", icon="🪣")


def settings() -> list[CliConfigSettingDefinition]:
    """Its settings (code options of the same keys are their defaults)."""
    restart = {"requires_restart": True}
    return [
        CliConfigSettingDefinition(key="bucket", label="Bucket", type="string", required=True, default="",
                                   validation=CliConfigValidation(max=63), **restart),
        CliConfigSettingDefinition(key="region", label="Region", type="string", default="us-east-1", **restart),
        CliConfigSettingDefinition(key="prefix", label="Key prefix", description="Prepended to every path", type="string", default="", **restart),
        CliConfigSettingDefinition(key="endpoint", label="Endpoint", description="An S3-compatible service (MinIO); empty: AWS",
                                   type="string", default="", **restart),
        CliConfigSettingDefinition(key="accessKeyId", label="Access key id", description="Empty: the AWS default credential chain",
                                   type="secret", **restart),
        CliConfigSettingDefinition(key="secretAccessKey", label="Secret access key", type="secret", **restart),
        CliConfigSettingDefinition(key="readOnly", label="Read-only", description="Uploads, new folders and deletes are refused",
                                   type="boolean", default=True, **restart),
    ]


async def create_plugin(context: Any) -> Any:
    if getattr(context.builder, "file_storage_provider", None) is not None:
        raise ValueError("the filesystem storage is already set by the host or another plugin")
    provider = await context.settings(
        title="Filesystem (S3)", description="An S3 or S3-compatible bucket served by the filesystem API", settings=settings(),
    )

    def text(key: str) -> str:
        return str(provider.value("settings", key) or "").strip()

    if not text("bucket"):
        return context.needs_configuration(provider.missing_required() or ["settings.bucket"])
    from .s3_provider import S3FileStorageProvider, S3ProviderOptions  # boto3, only once configured

    keys = bool(text("accessKeyId") and text("secretAccessKey"))
    storage = S3FileStorageProvider(S3ProviderOptions(
        bucket=text("bucket"), region=text("region") or "us-east-1", prefix=text("prefix") or None,
        endpoint_url=text("endpoint") or None,
        aws_access_key_id=text("accessKeyId") if keys else None, aws_secret_access_key=text("secretAccessKey") if keys else None,
    ))
    context.builder.set_file_storage_provider(
        storage if provider.value("settings", "readOnly") is False else read_only_file_storage(storage),
    )
    return None
