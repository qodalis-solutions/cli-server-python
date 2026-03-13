from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FileSystemOptions:
    """Configuration for the filesystem API.

    Attributes:
        allowed_paths: List of absolute directory paths that clients are
            permitted to access.  Every requested path is resolved and
            checked against this whitelist before any I/O is performed.
    """

    allowed_paths: list[str] = field(default_factory=list)
