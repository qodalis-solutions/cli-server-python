from __future__ import annotations


class FileStorageNotFoundError(Exception):
    """Raised when a path does not exist."""

    def __init__(self, path: str) -> None:
        super().__init__(f"Path not found: {path}")
        self.path = path


class FileStoragePermissionError(Exception):
    """Raised when access to a path is denied."""

    def __init__(self, path: str) -> None:
        super().__init__(f"Access denied: {path}")
        self.path = path


class FileStorageExistsError(Exception):
    """Raised when a path already exists and should not."""

    def __init__(self, path: str) -> None:
        super().__init__(f"Path already exists: {path}")
        self.path = path


class FileStorageNotADirectoryError(Exception):
    """Raised when a path is not a directory but should be."""

    def __init__(self, path: str) -> None:
        super().__init__(f"Not a directory: {path}")
        self.path = path


class FileStorageIsADirectoryError(Exception):
    """Raised when a path is a directory but should not be."""

    def __init__(self, path: str) -> None:
        super().__init__(f"Is a directory: {path}")
        self.path = path
