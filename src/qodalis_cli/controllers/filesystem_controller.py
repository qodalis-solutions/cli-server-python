from __future__ import annotations

import logging
import os
import shutil
import stat
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Union

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from qodalis_cli_filesystem import (
    FileStorageExistsError,
    FileStorageIsADirectoryError,
    FileStorageNotADirectoryError,
    FileStorageNotFoundError,
    FileStoragePermissionError,
    IFileStorageProvider,
)

from ..filesystem.filesystem_path_validator import FileSystemPathValidator

logger = logging.getLogger(__name__)


def _map_provider_error(exc: Exception) -> HTTPException:
    """Map a file-storage error to the appropriate HTTP exception."""
    if isinstance(exc, FileStorageNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, FileStoragePermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (FileStorageNotADirectoryError, FileStorageIsADirectoryError)):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, FileStorageExistsError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def create_filesystem_router(
    provider_or_validator: Union[IFileStorageProvider, FileSystemPathValidator],
) -> APIRouter:
    """Create a FastAPI router exposing filesystem operations.

    Accepts either a :class:`IFileStorageProvider` (new approach) or a
    :class:`FileSystemPathValidator` (legacy approach) for backward
    compatibility.
    """
    if isinstance(provider_or_validator, FileSystemPathValidator):
        return _create_legacy_router(provider_or_validator)

    return _create_provider_router(provider_or_validator)


def _create_provider_router(provider: IFileStorageProvider) -> APIRouter:
    """Create a router that delegates to *provider*."""

    router = APIRouter()

    @router.get("/ls")
    async def list_directory(path: str) -> dict[str, Any]:
        logger.debug("Listing directory: %s", path)
        try:
            entries = await provider.list(path)
            return {"entries": [asdict(e) for e in entries]}
        except FileStoragePermissionError as exc:
            logger.warning("Permission denied: %s", path)
            raise _map_provider_error(exc) from exc
        except (
            FileStorageNotFoundError,
            FileStorageNotADirectoryError,
        ) as exc:
            raise _map_provider_error(exc) from exc

    @router.get("/cat")
    async def read_file(path: str) -> dict[str, str]:
        logger.debug("Reading file: %s", path)
        try:
            content = await provider.read_file(path)
            return {"content": content}
        except FileStoragePermissionError as exc:
            logger.warning("Permission denied: %s", path)
            raise _map_provider_error(exc) from exc
        except (
            FileStorageNotFoundError,
            FileStorageIsADirectoryError,
        ) as exc:
            raise _map_provider_error(exc) from exc

    @router.get("/stat")
    async def stat_path(path: str) -> dict[str, Any]:
        logger.debug("Getting stat: %s", path)
        try:
            info = await provider.stat(path)
            return asdict(info)
        except FileStoragePermissionError as exc:
            logger.warning("Permission denied: %s", path)
            raise _map_provider_error(exc) from exc
        except (
            FileStorageNotFoundError,
        ) as exc:
            raise _map_provider_error(exc) from exc

    @router.get("/download")
    async def download_file(path: str) -> StreamingResponse:
        logger.debug("Downloading file: %s", path)
        try:
            stream = await provider.get_download_stream(path)
            filename = path.rstrip("/").rsplit("/", 1)[-1] or "download"
            return StreamingResponse(
                stream,
                media_type="application/octet-stream",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"'
                },
            )
        except FileStoragePermissionError as exc:
            logger.warning("Permission denied: %s", path)
            raise _map_provider_error(exc) from exc
        except (
            FileStorageNotFoundError,
            FileStorageIsADirectoryError,
        ) as exc:
            raise _map_provider_error(exc) from exc

    @router.post("/upload")
    async def upload_file(
        file: UploadFile = File(...),
        path: str = Form(...),
    ) -> dict[str, str]:
        logger.debug("Uploading file: %s", path)
        try:
            contents = await file.read()
            await provider.upload_file(path, contents)
            return {"path": path, "status": "uploaded"}
        except FileStoragePermissionError as exc:
            logger.warning("Permission denied: %s", path)
            raise _map_provider_error(exc) from exc
        except (
            FileStorageNotFoundError,
            FileStorageIsADirectoryError,
        ) as exc:
            raise _map_provider_error(exc) from exc

    @router.post("/mkdir")
    async def make_directory(body: dict[str, Any]) -> dict[str, str]:
        raw_path = body.get("path")
        if not raw_path:
            raise HTTPException(status_code=400, detail="'path' is required")

        logger.debug("Creating directory: %s", raw_path)
        recursive = body.get("recursive", True)
        try:
            await provider.mkdir(raw_path, recursive=recursive)
            return {"path": raw_path, "status": "created"}
        except FileStoragePermissionError as exc:
            logger.warning("Permission denied: %s", raw_path)
            raise _map_provider_error(exc) from exc
        except (
            FileStorageNotFoundError,
            FileStorageExistsError,
        ) as exc:
            raise _map_provider_error(exc) from exc

    @router.delete("/rm")
    async def remove_path(path: str, recursive: bool = True) -> dict[str, str]:
        logger.debug("Removing path: %s", path)
        try:
            await provider.remove(path, recursive=recursive)
            return {"path": path, "status": "deleted"}
        except FileStoragePermissionError as exc:
            logger.warning("Permission denied: %s", path)
            raise _map_provider_error(exc) from exc
        except (
            FileStorageNotFoundError,
        ) as exc:
            raise _map_provider_error(exc) from exc

    return router


def _safe_validate(validator: FileSystemPathValidator, path: str) -> str:
    """Validate *path* via the validator, raising a 403 on failure."""
    try:
        return validator.validate(path)
    except PermissionError as exc:
        logger.warning("Permission denied: %s", path)
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _stat_entry(resolved: str) -> dict[str, Any]:
    """Return a stat dictionary for the given resolved path."""
    st = os.stat(resolved)
    entry_type = "directory" if stat.S_ISDIR(st.st_mode) else "file"
    return {
        "name": os.path.basename(resolved),
        "type": entry_type,
        "size": st.st_size,
        "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        "created": datetime.fromtimestamp(st.st_ctime, tz=timezone.utc).isoformat(),
        "permissions": stat.filemode(st.st_mode),
    }


def _create_legacy_router(validator: FileSystemPathValidator) -> APIRouter:
    """Create a router using the legacy :class:`FileSystemPathValidator`."""

    router = APIRouter()

    @router.get("/ls")
    async def list_directory(path: str) -> dict[str, Any]:
        logger.debug("Listing directory: %s", path)
        resolved = _safe_validate(validator, path)

        if not os.path.exists(resolved):
            raise HTTPException(status_code=404, detail="Path not found")
        if not os.path.isdir(resolved):
            raise HTTPException(status_code=400, detail="Path is not a directory")

        entries: list[dict[str, Any]] = []
        try:
            for name in sorted(os.listdir(resolved)):
                full = os.path.join(resolved, name)
                try:
                    st = os.stat(full)
                    entry_type = "directory" if stat.S_ISDIR(st.st_mode) else "file"
                    entries.append(
                        {
                            "name": name,
                            "type": entry_type,
                            "size": st.st_size,
                            "modified": datetime.fromtimestamp(
                                st.st_mtime, tz=timezone.utc
                            ).isoformat(),
                            "permissions": stat.filemode(st.st_mode),
                        }
                    )
                except OSError:
                    continue
        except OSError as exc:
            logger.error("Failed to list directory %s: %s", path, str(exc))
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return {"entries": entries}

    @router.get("/cat")
    async def read_file(path: str) -> dict[str, str]:
        logger.debug("Reading file: %s", path)
        resolved = _safe_validate(validator, path)

        if not os.path.exists(resolved):
            raise HTTPException(status_code=404, detail="Path not found")
        if not os.path.isfile(resolved):
            raise HTTPException(status_code=400, detail="Path is not a file")

        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError as exc:
            logger.error("Failed to read file %s: %s", path, str(exc))
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return {"content": content}

    @router.get("/stat")
    async def stat_path(path: str) -> dict[str, Any]:
        logger.debug("Getting stat: %s", path)
        resolved = _safe_validate(validator, path)

        if not os.path.exists(resolved):
            raise HTTPException(status_code=404, detail="Path not found")

        try:
            return _stat_entry(resolved)
        except OSError as exc:
            logger.error("Failed to stat %s: %s", path, str(exc))
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.get("/download")
    async def download_file(path: str) -> FileResponse:
        logger.debug("Downloading file: %s", path)
        resolved = _safe_validate(validator, path)

        if not os.path.exists(resolved):
            raise HTTPException(status_code=404, detail="Path not found")
        if not os.path.isfile(resolved):
            raise HTTPException(status_code=400, detail="Path is not a file")

        filename = os.path.basename(resolved)
        return FileResponse(
            resolved,
            filename=filename,
            media_type="application/octet-stream",
        )

    @router.post("/upload")
    async def upload_file(
        file: UploadFile = File(...),
        path: str = Form(...),
    ) -> dict[str, str]:
        logger.debug("Uploading file: %s", path)
        resolved = _safe_validate(validator, path)

        parent = os.path.dirname(resolved)
        if not os.path.isdir(parent):
            raise HTTPException(
                status_code=400, detail="Parent directory does not exist"
            )

        try:
            contents = await file.read()
            with open(resolved, "wb") as fh:
                fh.write(contents)
        except OSError as exc:
            logger.error("Failed to upload file %s: %s", path, str(exc))
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return {"path": resolved, "status": "uploaded"}

    @router.post("/mkdir")
    async def make_directory(body: dict[str, str]) -> dict[str, str]:
        raw_path = body.get("path")
        if not raw_path:
            raise HTTPException(status_code=400, detail="'path' is required")

        logger.debug("Creating directory: %s", raw_path)
        resolved = _safe_validate(validator, raw_path)

        try:
            os.makedirs(resolved, exist_ok=True)
        except OSError as exc:
            logger.error("Failed to create directory %s: %s", raw_path, str(exc))
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return {"path": resolved, "status": "created"}

    @router.delete("/rm")
    async def remove_path(path: str) -> dict[str, str]:
        logger.debug("Removing path: %s", path)
        resolved = _safe_validate(validator, path)

        if not os.path.exists(resolved):
            raise HTTPException(status_code=404, detail="Path not found")

        try:
            if os.path.isdir(resolved):
                shutil.rmtree(resolved)
            else:
                os.remove(resolved)
        except OSError as exc:
            logger.error("Failed to remove %s: %s", path, str(exc))
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return {"path": resolved, "status": "deleted"}

    return router
