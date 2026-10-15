"""Input validation rules (security contract B7)."""

from __future__ import annotations

import json
import re
from typing import Any

from .errors import bad_request

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")
NAME_MAX = 64
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+$")
EMAIL_MAX = 254
PERMISSION_RE = re.compile(r"^(\*|[a-z0-9_.-]+(:([a-z0-9_.-]+|\*))*)$", re.IGNORECASE)
PERMISSION_MAX = 128
PERMISSIONS_PER_GROUP_MAX = 100
DESCRIPTION_MAX = 256
DATA_MAX_BYTES = 16 * 1024
BODY_MAX_BYTES = 1024 * 1024
PASSWORD_MAX = 1024

# Control characters, plus lone UTF-16 surrogates (valid in JSON escapes but
# not encodable as UTF-8: stored, they make every response containing them fail).
_CONTROL_RE = re.compile(r"[\u0000-\u001f\u007f\ud800-\udfff]")
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def has_control_chars(value: str) -> bool:
    return bool(_CONTROL_RE.search(value))


def has_lone_surrogates(value: str) -> bool:
    return bool(_SURROGATE_RE.search(value))


def sanitize(value: str | None, max_len: int | None = None) -> str | None:
    """Strip control characters (incl. ANSI ESC) and truncate."""
    if value is None:
        return None
    cleaned = _CONTROL_RE.sub("", value)
    return cleaned[:max_len] if max_len is not None else cleaned


def validate_name(value: str, kind: str = "name") -> str:
    if not value:
        raise bad_request(f"{kind} is required")
    if len(value) > NAME_MAX:
        raise bad_request(f"{kind} must be at most {NAME_MAX} characters")
    if not NAME_RE.match(value):
        raise bad_request(
            f"invalid {kind}: must start with a letter or digit and contain only "
            "letters, digits, '.', '_', '@' or '-'"
        )
    return value


def validate_email(value: str) -> str:
    if not value:
        raise bad_request("email is required")
    if len(value) > EMAIL_MAX or has_control_chars(value) or not EMAIL_RE.match(value):
        raise bad_request("invalid email")
    return value


def validate_text(value: str | None, field: str, max_len: int) -> str | None:
    if value is None:
        return None
    if has_control_chars(value):
        raise bad_request(f"{field} must not contain control characters")
    if len(value) > max_len:
        raise bad_request(f"{field} must be at most {max_len} characters")
    return value


def validate_permissions(permissions: list[str] | None) -> list[str] | None:
    if permissions is None:
        return None
    if len(permissions) > PERMISSIONS_PER_GROUP_MAX:
        raise bad_request(f"at most {PERMISSIONS_PER_GROUP_MAX} permissions per group")
    out: list[str] = []
    for raw in permissions:
        p = raw.strip()
        if not p or len(p) > PERMISSION_MAX or not PERMISSION_RE.match(p):
            raise bad_request(f"invalid permission: {sanitize(raw, PERMISSION_MAX)}")
        if p.lower() not in (x.lower() for x in out):
            out.append(p)
    return out


def validate_data(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if data is None:
        return None
    try:
        size = len(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise bad_request("invalid data") from exc
    if size > DATA_MAX_BYTES:
        raise bad_request(f"data must be at most {DATA_MAX_BYTES} bytes")
    return data
