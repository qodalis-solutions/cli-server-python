"""Resolves the dashboard dist directory at runtime."""

import os
from pathlib import Path


def resolve_dashboard_dir(explicit_path: str | None = None) -> str | None:
    """
    Resolve the dashboard dist directory. Looks for:
    1. Explicitly configured path
    2. node_modules/@qodalis/cli-server-dashboard/dist (npm package)
    3. Relative development paths (sibling repo)
    """
    # 1. Explicit override
    if explicit_path and os.path.isdir(explicit_path):
        return os.path.abspath(explicit_path)

    # 2. npm package — search upwards from cwd for node_modules
    current = os.getcwd()
    while current != os.path.dirname(current):
        candidate = os.path.join(
            current, "node_modules", "@qodalis", "cli-server-dashboard", "dist"
        )
        if os.path.isdir(candidate):
            return os.path.abspath(candidate)
        current = os.path.dirname(current)

    # 3. Relative development paths
    this_dir = os.path.dirname(os.path.abspath(__file__))
    dev_paths = [
        os.path.join(this_dir, "..", "..", "..", "..", "cli-server-dashboard", "dist"),
        os.path.join(os.getcwd(), "..", "cli-server-dashboard", "dist"),
    ]

    for path in dev_paths:
        if os.path.isdir(path):
            return os.path.abspath(path)

    return None
