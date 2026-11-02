"""Interfaces to the places a contact's profile data comes from."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from cyrano.domain.models import LeomatchSourceText, TelegramProfile


class ProfileSourceError(Exception):
    pass


class ProfileSource(Protocol):
    async def fetch_profile(self, tg_id: int) -> TelegramProfile: ...

    async def download_photo(self, tg_id: int, destination: Path) -> bool: ...


class CardPhoto(Protocol):
    async def download(self, destination: Path) -> bool: ...


@dataclass(frozen=True, slots=True)
class LeomatchLookup:
    texts: list[LeomatchSourceText] = field(default_factory=list)
    card_photo: CardPhoto | None = None


class LeomatchSource(Protocol):
    async def find_texts(self, tg_id: int, username: str | None) -> LeomatchLookup: ...
