"""In-memory implementation of ICredentialStore."""

from __future__ import annotations

import time

from ..models import Credentials
from .interfaces import ICredentialStore


class InMemoryCredentialStore(ICredentialStore):
    def __init__(self) -> None:
        self._credentials: dict[str, Credentials] = {}

    async def set_password(self, user_id: str, hash_: str, salt: str) -> None:
        self._credentials[user_id] = Credentials(
            user_id=user_id,
            password_hash=hash_,
            salt=salt,
            last_changed=int(time.time() * 1000),
        )

    async def get_credentials(self, user_id: str) -> Credentials | None:
        return self._credentials.get(user_id)

    async def delete_credentials(self, user_id: str) -> None:
        self._credentials.pop(user_id, None)
