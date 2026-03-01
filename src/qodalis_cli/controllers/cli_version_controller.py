from __future__ import annotations

import os
import platform
from typing import Any

from fastapi import APIRouter

SERVER_VERSION = "1.0.0"


def create_cli_version_router() -> APIRouter:
    router = APIRouter()

    @router.get("/versions")
    async def get_versions() -> dict[str, Any]:
        return {
            "supportedVersions": [1, 2],
            "preferredVersion": 2,
            "serverVersion": SERVER_VERSION,
        }

    @router.get("/capabilities")
    async def get_capabilities() -> dict[str, Any]:
        detected_os = (
            "darwin" if platform.system() == "Darwin"
            else "win32" if platform.system() == "Windows"
            else "linux"
        )

        shell = "powershell" if platform.system() == "Windows" else "bash"
        shell_path = (
            "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
            if platform.system() == "Windows"
            else "/bin/bash"
        )

        return {
            "shell": True,
            "os": detected_os,
            "shellPath": shell_path,
            "version": SERVER_VERSION,
        }

    return router
