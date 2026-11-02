from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from cyrano.domain.models import (
    ContactCard,
    LeomatchSourceText,
    Mode,
    ModeChoice,
    ModeCreate,
    TelegramProfile,
)
from cyrano.services.profile_source import CardPhoto, LeomatchLookup

FAKE_MODE_TIME = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
FAKE_PHOTO = b"fake-jpeg-bytes"
FAKE_BIO = "fake bio text"
FAKE_CARD_PHOTO = b"fake-card-jpeg-bytes"


def fake_telegram_profile(**overrides: str | None) -> TelegramProfile:
    fields: dict[str, str | None] = {
        "username": "fake_fresh",
        "display_name": "Freshfirst Freshlast",
        "bio": FAKE_BIO,
    }
    return TelegramProfile.model_validate({**fields, **overrides})


class FakeProfileSource:
    def __init__(
        self,
        *,
        profile: TelegramProfile | None = None,
        photo: bytes | None = FAKE_PHOTO,
        fetch_error: Exception | None = None,
        photo_error: Exception | None = None,
        fetch_delay: float = 0.0,
        photo_delay: float = 0.0,
        failing_ids: frozenset[int] = frozenset(),
    ) -> None:
        self.profile = profile if profile is not None else fake_telegram_profile()
        self.photo = photo
        self.fetched: list[int] = []
        self.downloaded: list[tuple[int, Path]] = []
        self.active = 0
        self.max_active = 0
        self._fetch_error = fetch_error
        self._photo_error = photo_error
        self._fetch_delay = fetch_delay
        self._photo_delay = photo_delay
        self._failing_ids = failing_ids

    @asynccontextmanager
    async def _tracked(self) -> AsyncIterator[None]:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            yield
        finally:
            self.active -= 1

    async def fetch_profile(self, tg_id: int) -> TelegramProfile:
        async with self._tracked():
            self.fetched.append(tg_id)
            await asyncio.sleep(self._fetch_delay)
            if tg_id in self._failing_ids:
                raise RuntimeError("fake failure for one contact")
            if self._fetch_error is not None:
                raise self._fetch_error
            return self.profile

    async def download_photo(self, tg_id: int, destination: Path) -> bool:
        async with self._tracked():
            self.downloaded.append((tg_id, destination))
            await asyncio.sleep(self._photo_delay)
            if self._photo_error is not None:
                if self.photo is not None:
                    await asyncio.to_thread(destination.write_bytes, b"partial")
                raise self._photo_error
            if self.photo is None:
                return False
            await asyncio.to_thread(destination.write_bytes, self.photo)
            return True


class FakeCardPhoto:
    def __init__(
        self,
        content: bytes | None = FAKE_CARD_PHOTO,
        *,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.destinations: list[Path] = []
        self._content = content
        self._error = error
        self._delay = delay

    async def download(self, destination: Path) -> bool:
        self.destinations.append(destination)
        await asyncio.sleep(self._delay)
        if self._error is not None:
            await asyncio.to_thread(destination.write_bytes, b"partial")
            raise self._error
        if self._content is None:
            return False
        await asyncio.to_thread(destination.write_bytes, self._content)
        return True


class FakeLeomatchSource:
    def __init__(
        self,
        texts: list[LeomatchSourceText] | None = None,
        *,
        card_photo: CardPhoto | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.texts = texts if texts is not None else []
        self.card_photo = card_photo
        self.queries: list[tuple[int, str | None]] = []
        self._error = error
        self._delay = delay

    async def find_texts(self, tg_id: int, username: str | None) -> LeomatchLookup:
        self.queries.append((tg_id, username))
        await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return LeomatchLookup(self.texts, self.card_photo)


class InMemoryModes:
    def __init__(self) -> None:
        self.stored: dict[int, Mode] = {}

    async def get_by_id(self, mode_id: int) -> Mode | None:
        return self.stored.get(mode_id)

    async def get_by_name(self, name: str) -> Mode | None:
        return next((m for m in self.stored.values() if m.name == name), None)

    async def list_enabled(self) -> list[Mode]:
        return [m for m in self.stored.values() if m.enabled]

    async def create(self, data: ModeCreate) -> Mode:
        mode = Mode(
            id=len(self.stored) + 1,
            name=data.name,
            prompt_fragment=data.prompt_fragment,
            few_shot_examples=data.few_shot_examples,
            enabled=data.enabled,
            created_at=FAKE_MODE_TIME,
            updated_at=FAKE_MODE_TIME,
        )
        self.stored[mode.id] = mode
        return mode

    async def resolve_by_id(self, mode_id: int | None) -> Mode:
        raise NotImplementedError

    async def resolve_by_name(self, name: str) -> Mode:
        raise NotImplementedError


class FakeCardSender:
    def __init__(
        self,
        *,
        results: list[bool] | None = None,
        error: Exception | None = None,
        failing_ids: frozenset[int] = frozenset(),
        delay: float = 0.0,
    ) -> None:
        self.cards: list[ContactCard] = []
        self.active = 0
        self.max_active = 0
        self._results = results if results is not None else []
        self._error = error
        self._failing_ids = failing_ids
        self._delay = delay

    async def send_card(self, card: ContactCard) -> bool:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.cards.append(card)
            await asyncio.sleep(self._delay)
            if card.contact_id in self._failing_ids:
                raise RuntimeError("fake failure for one contact")
            if self._error is not None:
                raise self._error
            return self._results.pop(0) if self._results else True
        finally:
            self.active -= 1


def fake_card(**overrides: object) -> ContactCard:
    fields: dict[str, object] = {
        "contact_id": 17,
        "display_name": "Fakefirst Fakelast",
        "username": "fake_user",
        "modes": [ModeChoice(id=1, name="default"), ModeChoice(id=2, name="flirt")],
        "profile_collected": True,
    }
    return ContactCard.model_validate({**fields, **overrides})
