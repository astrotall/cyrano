from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Protocol

from telethon import events

from cyrano.domain.enums import ManualMessageOutcome
from cyrano.domain.models import OutgoingMessage
from cyrano.userbot.filters import Rejection, outgoing_event_rejection, sender_rejection
from cyrano.userbot.protocols import EventCallback, IncomingEvent, UserbotClient

logger = logging.getLogger("cyrano.userbot.outgoing_handler")

DETECTION_RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.0, 2.0)


class OutgoingProcessor(Protocol):
    async def handle(self, outgoing: OutgoingMessage) -> ManualMessageOutcome: ...


class OutgoingHandler:
    def __init__(
        self,
        detector: OutgoingProcessor,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._detector = detector
        self._sleep = sleep
        self._callback: EventCallback = self.handle
        self._handled = 0
        self._failed = 0
        self._rejected: Counter[Rejection] = Counter()

    @property
    def handled(self) -> int:
        return self._handled

    @property
    def failed(self) -> int:
        return self._failed

    @property
    def rejected(self) -> int:
        return self._rejected.total()

    def register(self, client: UserbotClient) -> None:
        client.add_event_handler(self._callback, events.NewMessage(outgoing=True))

    def unregister(self, client: UserbotClient) -> None:
        client.remove_event_handler(self._callback)

    async def handle(self, event: IncomingEvent) -> None:
        try:
            await self._process(event)
        except Exception as exc:
            self._failed += 1
            logger.error(
                "outgoing message failed",
                extra={"error": type(exc).__name__, "failed": self._failed},
            )

    async def _process(self, event: IncomingEvent) -> None:
        reason = outgoing_event_rejection(event)
        if reason is not None:
            self._reject(reason)
            return
        await self._detect_with_retry(event)

    async def _detect_with_retry(self, event: IncomingEvent) -> None:
        for attempt, delay in enumerate(DETECTION_RETRY_DELAYS_SECONDS, start=1):
            try:
                await self._detect(event)
            except Exception as exc:
                logger.warning(
                    "detection attempt failed, retrying",
                    extra={"error": type(exc).__name__, "attempt": attempt, "delay": delay},
                )
                await self._sleep(delay)
            else:
                return
        await self._detect(event)

    async def _detect(self, event: IncomingEvent) -> None:
        peer = await event.get_chat()
        if peer is None:
            self._reject(Rejection.NO_PEER)
            return
        reason = sender_rejection(peer)
        if reason is not None:
            self._reject(reason)
            return
        outcome = await self._detector.handle(
            OutgoingMessage(
                tg_id=peer.id,
                tg_message_id=event.id,
                text=event.raw_text or "",
                sent_at=event.date,
            )
        )
        self._handled += 1
        logger.debug("outgoing message processed", extra={"outcome": outcome.value})

    def _reject(self, reason: Rejection) -> None:
        self._rejected[reason] += 1
        logger.debug(
            "outgoing event rejected",
            extra={"reason": reason.value, "rejected": self.rejected},
        )
