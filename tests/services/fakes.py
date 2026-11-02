from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from cyrano.domain.models import LeomatchSourceText, TelegramProfile

FAKE_PHOTO = b"fake-jpeg-bytes"
FAKE_BIO = "fake bio text"


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


class FakeLeomatchSource:
    def __init__(
        self,
        texts: list[LeomatchSourceText] | None = None,
        *,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.texts = texts if texts is not None else []
        self.queries: list[tuple[int, str | None]] = []
        self._error = error
        self._delay = delay

    async def find_texts(self, tg_id: int, username: str | None) -> list[LeomatchSourceText]:
        self.queries.append((tg_id, username))
        await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self.texts
