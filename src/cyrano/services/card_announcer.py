from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from cyrano.domain.enums import ContactStatus
from cyrano.domain.models import CARD_MESSAGES_LIMIT, Contact, ContactReadyEvent
from cyrano.services.card_builder import build_card, profile_of
from cyrano.services.card_sender import CardSender
from cyrano.services.contact_events import ContactEventSubscriber
from cyrano.services.repositories import ContactRepository, MessageRepository, ModeRepository

logger = logging.getLogger("cyrano.services.card_announcer")

CARD_SWEEP_INTERVAL = 120.0
CARD_WITHOUT_PROFILE_DELAY = 600.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CardAnnouncer:
    def __init__(
        self,
        contacts: ContactRepository,
        messages: MessageRepository,
        modes: ModeRepository,
        sender: CardSender,
        ready: ContactEventSubscriber[ContactReadyEvent],
        *,
        clock: Callable[[], datetime] = _utc_now,
        sweep_interval: float = CARD_SWEEP_INTERVAL,
        without_profile_delay: float = CARD_WITHOUT_PROFILE_DELAY,
    ) -> None:
        self._contacts = contacts
        self._messages = messages
        self._modes = modes
        self._sender = sender
        self._ready = ready
        self._clock = clock
        self._sweep_interval = sweep_interval
        self._without_profile_delay = timedelta(seconds=without_profile_delay)
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._queued: set[int] = set()
        self._sweeps = 0
        self._warned_in_sweep = -1
        self._announced = 0
        self._deferred = 0
        self._skipped = 0
        self._failed = 0

    @property
    def announced(self) -> int:
        return self._announced

    @property
    def deferred(self) -> int:
        return self._deferred

    @property
    def skipped(self) -> int:
        return self._skipped

    @property
    def failed(self) -> int:
        return self._failed

    async def run(self) -> None:
        feeders = [
            asyncio.create_task(self._feed_ready()),
            asyncio.create_task(self._feed_sweeps()),
        ]
        try:
            while True:
                contact_id = await self._queue.get()
                try:
                    await self._announce_safely(contact_id)
                finally:
                    self._queued.discard(contact_id)
        finally:
            for feeder in feeders:
                feeder.cancel()
            await asyncio.gather(*feeders, return_exceptions=True)

    def _enqueue(self, contact_id: int) -> None:
        if contact_id not in self._queued:
            self._queued.add(contact_id)
            self._queue.put_nowait(contact_id)

    async def _feed_ready(self) -> None:
        async for event in self._ready.subscribe():
            self._enqueue(event.contact_id)

    async def _feed_sweeps(self) -> None:
        while True:
            await self._sweep()
            await asyncio.sleep(self._sweep_interval)

    def _is_due(self, contact: Contact) -> bool:
        if profile_of(contact).collected_at is not None:
            return True
        return self._clock() - contact.created_at >= self._without_profile_delay

    async def _sweep(self) -> None:
        self._sweeps += 1
        try:
            waiting = await self._contacts.list_by_status(ContactStatus.NEW)
        except Exception as exc:
            self._failed += 1
            self._warn("card sweep failed", exc)
            return
        for contact in waiting:
            if self._is_due(contact):
                self._enqueue(contact.id)

    async def _announce_safely(self, contact_id: int) -> None:
        try:
            await self._announce(contact_id)
        except Exception as exc:
            self._failed += 1
            self._warn("card announcement failed", exc)

    async def _announce(self, contact_id: int) -> None:
        contact = await self._contacts.get_by_id(contact_id)
        if contact is None or contact.status is not ContactStatus.NEW:
            self._skipped += 1
            return
        messages = await self._messages.list_last(contact.id, CARD_MESSAGES_LIMIT)
        modes = await self._modes.list_enabled()
        delivered = await self._sender.send_card(build_card(contact, messages, modes))
        if not delivered:
            self._deferred += 1
            self._warn("card was not delivered, the next sweep retries it")
            return
        await self._contacts.set_status(contact.id, ContactStatus.PENDING)
        self._announced += 1
        logger.info("card announced", extra={"contact_id": contact.id})

    def _warn(self, message: str, error: Exception | None = None) -> None:
        if self._warned_in_sweep == self._sweeps:
            return
        self._warned_in_sweep = self._sweeps
        extra: dict[str, int | str] = {
            "announced": self._announced,
            "deferred": self._deferred,
            "failed": self._failed,
        }
        if error is not None:
            extra["error"] = type(error).__name__
        logger.warning(message, extra=extra)
