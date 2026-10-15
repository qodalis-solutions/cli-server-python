"""In-memory implementation of IApiTokenStore."""

from __future__ import annotations

import copy

from ..models import ApiToken
from .interfaces import IApiTokenStore


class InMemoryApiTokenStore(IApiTokenStore):
    def __init__(self) -> None:
        self._tokens: dict[str, ApiToken] = {}  # keyed by id

    async def create_token(self, token: ApiToken) -> ApiToken:
        self._tokens[token.id] = copy.deepcopy(token)
        return copy.deepcopy(token)

    async def get_token_by_hash(self, token_hash: str) -> ApiToken | None:
        for token in self._tokens.values():
            if token.token_hash == token_hash:
                return copy.deepcopy(token)
        return None

    async def get_token(self, token_id: str) -> ApiToken | None:
        token = self._tokens.get(token_id)
        return copy.deepcopy(token) if token else None

    async def get_tokens(self, user_id: str | None = None) -> list[ApiToken]:
        return [
            copy.deepcopy(t) for t in self._tokens.values()
            if user_id is None or t.user_id == user_id
        ]

    async def delete_token(self, token_id: str) -> None:
        self._tokens.pop(token_id, None)

    async def delete_user_tokens(self, user_id: str) -> None:
        for token_id, token in list(self._tokens.items()):
            if token.user_id == user_id:
                del self._tokens[token_id]

    async def update_last_used(self, token_id: str, last_used_at: int) -> None:
        token = self._tokens.get(token_id)
        if token:
            token.last_used_at = last_used_at
