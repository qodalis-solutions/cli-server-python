from __future__ import annotations

from dataclasses import dataclass, field

#: Default cap for one ``POST /api/qcli/fs/upload`` file (100 MiB).
DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


@dataclass
class FileSystemOptions:
    """Configuration for the filesystem API.

    Attributes:
        allowed_paths: List of absolute directory paths that clients are
            permitted to access.  Every requested path is resolved and
            checked against this whitelist before any I/O is performed.
        max_upload_bytes: Largest file accepted by ``POST /upload``; a
            bigger upload is answered with 413 and never buffered beyond
            the cap.  Also applies with a custom file storage provider.
    """

    allowed_paths: list[str] = field(default_factory=list)
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
