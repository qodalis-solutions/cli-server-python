"""The marketplace's plugins folders: a file system error on one becomes a message naming the folder, the reason and
what to set, and a folder that cannot be created or written is found at startup (the same messages as the Node and
.NET servers)."""

from __future__ import annotations

import errno
import os
from typing import Optional, Sequence

#: The environment variable that moves the server's data directory when ``data_dir`` is not given.
QCLI_DATA_DIR_ENV = "QCLI_DATA_DIR"

#: What to do about an unwritable folder below the server's data directory.
DATA_DIR_HINT = f"Set data_dir (or {QCLI_DATA_DIR_ENV}) to a writable folder"

_REASONS = {
    "EACCES": "permission denied",
    "EPERM": "operation not permitted",
    "EROFS": "read-only file system",
    "ENOSPC": "no space left on device",
    "EDQUOT": "disk quota exceeded",
    "ENOENT": "no such file or directory",
    "ENOTDIR": "not a directory",
    "EISDIR": "is a directory",
    "EEXIST": "file already exists",
    "EMFILE": "too many open files",
    "EBUSY": "resource busy or locked",
}


def file_system_reason(code: str) -> str:
    """``permission denied (EACCES)``: a file system error code in words."""
    return f"{_REASONS.get(code, 'file system error')} ({code})"


def plugins_folder_message(folder: str, code: str, hint: str) -> str:
    """The message of an unwritable plugins folder, the same on every server."""
    return f"cannot write the plugins folder {folder}: {file_system_reason(code)}. {hint}"


def _code_of(exc: OSError) -> Optional[str]:
    return errno.errorcode.get(exc.errno) if isinstance(exc.errno, int) else None


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep) or root.startswith(path + os.sep)


def plugins_folder_error(exc: BaseException, folder: str, hint: str) -> Optional[str]:
    """The message for a file system error on ``folder``, one of its files or one of its parents (``makedirs`` fails
    on the first parent it cannot create); ``None`` for anything else.  Only the folder, the error code and the hint
    are named: never the error's own text."""
    if not isinstance(exc, OSError) or not isinstance(exc.filename, str):
        return None
    code = _code_of(exc)
    if code is None:
        return None
    root = os.path.abspath(folder)
    paths = [os.path.abspath(p) for p in (exc.filename, exc.filename2) if isinstance(p, str)]
    if not any(_inside(p, root) for p in paths):
        return None
    return plugins_folder_message(root, code, hint)


def unwritable_folder_code(folder: str) -> Optional[str]:
    """Why ``folder`` cannot be created or written, without creating it: the error code of the folder, or of its
    nearest existing parent when it does not exist yet; ``None`` when it can be."""
    path = os.path.abspath(folder)
    while True:
        if os.path.exists(path):
            if not os.path.isdir(path):
                return "ENOTDIR"
            return None if os.access(path, os.W_OK | os.X_OK) else "EACCES"
        parent = os.path.dirname(path)
        if parent == path:
            return "ENOENT"
        path = parent


def unwritable_folders_warning(folders: Sequence[tuple[str, str]]) -> Optional[str]:
    """The startup WARNING for the marketplace folders (``(dir, hint)``) that cannot be created or written; ``None``
    when every folder is fine."""
    bad = [(os.path.abspath(d), h, c) for d, h in folders if (c := unwritable_folder_code(d))]
    if not bad:
        return None
    hints = "; ".join(dict.fromkeys(h for _, h, _ in bad))
    listing = "; ".join(f"{d}: {file_system_reason(c)}" for d, _, c in bad)
    plural = "s" if len(bad) > 1 else ""
    return (
        f"the marketplace cannot write the plugins folder{plural} {listing}. "
        f"Installing, updating and removing plugins will fail. {hints}."
    )
