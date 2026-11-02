"""Interfaces to the places a contact's profile data comes from."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from cyrano.domain.models import LeomatchSourceText, TelegramProfile


class ProfileSourceError(Exception):
    pass


class ProfileSource(Protocol):
    async def fetch_profile(self, tg_id: int) -> TelegramProfile: ...

    async def download_photo(self, tg_id: int, destination: Path) -> bool: ...


class LeomatchSource(Protocol):
    async def find_texts(self, tg_id: int, username: str | None) -> list[LeomatchSourceText]: ...
