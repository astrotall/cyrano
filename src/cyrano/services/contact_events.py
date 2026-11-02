"""Best-effort notifications about newly registered contacts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Protocol

from cyrano.domain.models import NewContactEvent

logger = logging.getLogger("cyrano.services.contact_events")

EVENT_QUEUE_SIZE = 1000


class ContactEventPublisher(Protocol):
    async def publish(self, event: NewContactEvent) -> None: ...


class InMemoryContactEvents:
    def __init__(self, max_size: int = EVENT_QUEUE_SIZE) -> None:
        self._queue: asyncio.Queue[NewContactEvent] = asyncio.Queue(maxsize=max_size)
        self._published = 0
        self._dropped = 0

    @property
    def published(self) -> int:
        return self._published

    @property
    def dropped(self) -> int:
        return self._dropped

    async def publish(self, event: NewContactEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1
            logger.warning(
                "contact event dropped, queue is full",
                extra={"dropped": self._dropped, "queued": self._queue.qsize()},
            )
            return
        self._published += 1

    async def subscribe(self) -> AsyncIterator[NewContactEvent]:
        while True:
            yield await self._queue.get()
