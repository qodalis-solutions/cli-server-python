from __future__ import annotations

import os


class FileSystemPathValidator:
    """Validates that requested paths fall within an allowed whitelist.

    Every path is resolved to its real, absolute form (following symlinks)
    before the check, which prevents path-traversal attacks.
    """

    def __init__(self, allowed_paths: list[str]) -> None:
        # Pre-resolve allowed roots so comparisons are consistent.
        self._allowed_paths: list[str] = [
            os.path.realpath(p) for p in allowed_paths
        ]

    @property
    def allowed_paths(self) -> list[str]:
        return list(self._allowed_paths)

    def validate(self, path: str) -> str:
        """Resolve *path* and verify it lives under an allowed root.

        Returns:
            The resolved absolute path on success.

        Raises:
            PermissionError: If the resolved path is not within any
                allowed directory.
        """
        resolved = os.path.realpath(path)

        for allowed in self._allowed_paths:
            # Use os.sep to ensure "/tmp/evil" doesn't match "/tmp/ev"
            if resolved == allowed or resolved.startswith(allowed + os.sep):
                return resolved

        raise PermissionError(
            f"Access denied: '{path}' is outside the allowed paths"
        )
