"""Resolves the dashboard dist directory at runtime."""

import os
from pathlib import Path


def resolve_dashboard_dir(explicit_path: str | None = None) -> str | None:
    """Resolve the dashboard dist directory.

    Searches in order: explicit path, bundled directory, npm package
    (``node_modules/@qodalis/cli-server-dashboard/dist``), and relative
    development paths.

    Returns:
        The absolute path to the dashboard directory, or ``None``.
    """
    if explicit_path and os.path.isdir(explicit_path):
        return os.path.abspath(explicit_path)

    bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dashboard')
    if os.path.isdir(bundled):
        return bundled

    current = os.getcwd()
    while current != os.path.dirname(current):
        candidate = os.path.join(
            current, "node_modules", "@qodalis", "cli-server-dashboard", "dist"
        )
        if os.path.isdir(candidate):
            return os.path.abspath(candidate)
        current = os.path.dirname(current)

    this_dir = os.path.dirname(os.path.abspath(__file__))
    dev_paths = [
        os.path.join(this_dir, "..", "..", "..", "..", "cli-server-dashboard", "dist"),
        os.path.join(os.getcwd(), "..", "cli-server-dashboard", "dist"),
    ]

    for path in dev_paths:
        if os.path.isdir(path):
            return os.path.abspath(path)

    return None
