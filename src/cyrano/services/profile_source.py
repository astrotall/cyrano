"""Interfaces to the places a contact's profile data comes from."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cyrano.domain.models import TelegramProfile


class ProfileSourceError(Exception):
    pass


class ProfileSource(Protocol):
    async def fetch_profile(self, tg_id: int) -> TelegramProfile: ...

    async def download_photo(self, tg_id: int, destination: Path) -> bool: ...


class CardPhotos(Protocol):
    @property
    def count(self) -> int: ...

    async def download(self, index: int, destination: Path) -> bool: ...


@dataclass(frozen=True, slots=True)
class LeomatchLookup:
    card_text: str | None = None
    match_text: str | None = None
    match_name: str | None = None
    card_photos: CardPhotos | None = None


class LeomatchSource(Protocol):
    async def find_texts(self, tg_id: int, username: str | None) -> LeomatchLookup: ...
