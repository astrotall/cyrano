"""Collection of everything known about a new contact, so the owner can decide from one card."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from cyrano.domain.enums import ContactStatus
from cyrano.domain.models import (
    MAX_BIO_LENGTH,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_USERNAME_LENGTH,
    Contact,
    ContactProfile,
    ContactReadyEvent,
    ContactUpdate,
    LeomatchProfile,
    NewContactEvent,
)
from cyrano.services.contact_events import ContactEventPublisher, ContactEventSubscriber
from cyrano.services.leomatch import build_leomatch_profile
from cyrano.services.profile_source import LeomatchSource, ProfileSource
from cyrano.services.repositories import ContactRepository
from cyrano.services.sanitize import clean_text

logger = logging.getLogger("cyrano.services.profile_collector")

COLLECTION_TIMEOUT_SECONDS = 30.0
PROFILE_PHOTO_DIRECTORY = "profiles"
PROFILE_PHOTO_SUFFIX = ".jpg"
PARTIAL_PHOTO_MARKER = ".part"


class CollectionPart(StrEnum):
    TELEGRAM = "telegram"
    PHOTO = "photo"
    LEOMATCH = "leomatch"


@dataclass(slots=True)
class _Gathered:
    username: str | None
    display_name: str | None
    bio: str | None
    photo: str | None
    leomatch: LeomatchProfile | None
    telegram_collected: bool = False


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _profile_of(contact: Contact) -> ContactProfile:
    try:
        return ContactProfile.model_validate(contact.profile)
    except ValidationError:
        return ContactProfile()


class ProfileCollector:
    def __init__(
        self,
        contacts: ContactRepository,
        profiles: ProfileSource,
        leomatch: LeomatchSource,
        new_contacts: ContactEventSubscriber[NewContactEvent],
        ready: ContactEventPublisher[ContactReadyEvent],
        media_dir: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
        timeout_seconds: float = COLLECTION_TIMEOUT_SECONDS,
    ) -> None:
        self._contacts = contacts
        self._profiles = profiles
        self._leomatch = leomatch
        self._new_contacts = new_contacts
        self._ready = ready
        self._media_dir = media_dir
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        self._finished = 0
        self._failed = 0

    @property
    def finished(self) -> int:
        return self._finished

    @property
    def failed(self) -> int:
        return self._failed

    async def run(self) -> None:
        await self._sweep()
        async for event in self._new_contacts.subscribe():
            await self._process_safely(event.contact_id)

    async def _sweep(self) -> None:
        try:
            waiting = await self._contacts.list_by_status(ContactStatus.NEW)
        except Exception as exc:
            self._failed += 1
            logger.error(
                "profile sweep failed", extra={"error": type(exc).__name__, "failed": self._failed}
            )
            return
        uncollected = [c.id for c in waiting if _profile_of(c).collected_at is None]
        if uncollected:
            logger.info("profile sweep started", extra={"contacts": len(uncollected)})
        for contact_id in uncollected:
            await self._process_safely(contact_id)

    async def _process_safely(self, contact_id: int) -> None:
        try:
            await self._process(contact_id)
        except Exception as exc:
            self._failed += 1
            logger.error(
                "profile collection failed",
                extra={"error": type(exc).__name__, "failed": self._failed},
            )

    async def _process(self, contact_id: int) -> None:
        attempted = True
        try:
            attempted = await self._collect(contact_id)
        finally:
            if attempted:
                await self._ready.publish(ContactReadyEvent(contact_id=contact_id))

    async def _collect(self, contact_id: int) -> bool:
        contact = await self._contacts.get_by_id(contact_id)
        if contact is None:
            return False
        profile = _profile_of(contact)
        if profile.collected_at is not None:
            return False
        gathered = _Gathered(
            username=profile.username or contact.username,
            display_name=profile.display_name or contact.display_name,
            bio=profile.bio,
            photo=profile.photo,
            leomatch=profile.leomatch,
        )
        try:
            await asyncio.wait_for(self._gather(contact, gathered), self._timeout_seconds)
        except TimeoutError:
            logger.warning("profile collection timed out", extra={"contact_id": contact.id})
        await self._store(contact, gathered)
        self._finished += 1
        logger.info(
            "profile collection finished",
            extra={
                "contact_id": contact.id,
                "telegram": gathered.telegram_collected,
                "photo": gathered.photo is not None,
                "leomatch": gathered.leomatch is not None,
            },
        )
        return True

    async def _gather(self, contact: Contact, gathered: _Gathered) -> None:
        await self._gather_telegram(contact, gathered)
        await self._gather_photo(contact, gathered)
        await self._gather_leomatch(contact, gathered)

    def _part_failed(self, contact: Contact, part: CollectionPart, error: Exception) -> None:
        logger.warning(
            "profile part failed",
            extra={"contact_id": contact.id, "part": part.value, "error": type(error).__name__},
        )

    async def _gather_telegram(self, contact: Contact, gathered: _Gathered) -> None:
        try:
            fetched = await self._profiles.fetch_profile(contact.tg_id)
        except Exception as exc:
            self._part_failed(contact, CollectionPart.TELEGRAM, exc)
            return
        gathered.username = clean_text(fetched.username, MAX_USERNAME_LENGTH)
        gathered.display_name = clean_text(fetched.display_name, MAX_DISPLAY_NAME_LENGTH)
        gathered.bio = clean_text(fetched.bio, MAX_BIO_LENGTH)
        gathered.telegram_collected = True

    async def _gather_photo(self, contact: Contact, gathered: _Gathered) -> None:
        name = f"{contact.id}{PROFILE_PHOTO_SUFFIX}"
        relative = Path(PROFILE_PHOTO_DIRECTORY) / name
        final = self._media_dir / relative
        partial = final.with_name(f"{contact.id}{PARTIAL_PHOTO_MARKER}{PROFILE_PHOTO_SUFFIX}")
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            if await self._profiles.download_photo(contact.tg_id, partial):
                partial.replace(final)
                gathered.photo = relative.as_posix()
        except Exception as exc:
            self._part_failed(contact, CollectionPart.PHOTO, exc)
        finally:
            with contextlib.suppress(OSError):
                partial.unlink(missing_ok=True)

    async def _gather_leomatch(self, contact: Contact, gathered: _Gathered) -> None:
        try:
            texts = await self._leomatch.find_texts(contact.tg_id, gathered.username)
            found = build_leomatch_profile(texts)
        except Exception as exc:
            self._part_failed(contact, CollectionPart.LEOMATCH, exc)
            return
        if found is not None:
            gathered.leomatch = found

    async def _store(self, contact: Contact, gathered: _Gathered) -> None:
        profile = ContactProfile(
            username=gathered.username,
            display_name=gathered.display_name,
            bio=gathered.bio,
            photo=gathered.photo,
            leomatch=gathered.leomatch,
            collected_at=self._clock() if gathered.telegram_collected else None,
        )
        if gathered.telegram_collected:
            await self._contacts.update_identity(
                contact.id, gathered.username, gathered.display_name
            )
        await self._contacts.update(
            contact.id, ContactUpdate(profile=profile.model_dump(mode="json"))
        )
