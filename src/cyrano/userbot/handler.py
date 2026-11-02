"""The incoming-message handler: Telethon event in, IncomingMessage to the intake out."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Protocol

from telethon import events

from cyrano.domain.models import IncomingMessage
from cyrano.services.intake import IntakeResult
from cyrano.userbot.filters import Rejection, event_rejection, sender_rejection
from cyrano.userbot.protocols import EventCallback, IncomingEvent, SenderLike, UserbotClient

logger = logging.getLogger("cyrano.userbot.handler")

INTAKE_RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.0, 2.0)


class IncomingProcessor(Protocol):
    async def handle(self, incoming: IncomingMessage) -> IntakeResult: ...


def display_name_of(sender: SenderLike) -> str | None:
    parts = (part.strip() for part in (sender.first_name, sender.last_name) if part)
    return " ".join(part for part in parts if part) or None


def to_incoming_message(event: IncomingEvent, sender: SenderLike) -> IncomingMessage:
    return IncomingMessage(
        tg_id=sender.id,
        username=sender.username or None,
        display_name=display_name_of(sender),
        tg_message_id=event.id,
        text=event.raw_text or "",
        sent_at=event.date,
    )


class IncomingHandler:
    def __init__(
        self,
        intake: IncomingProcessor,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._intake = intake
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
        client.add_event_handler(self._callback, events.NewMessage(incoming=True))

    def unregister(self, client: UserbotClient) -> None:
        client.remove_event_handler(self._callback)

    async def handle(self, event: IncomingEvent) -> None:
        try:
            await self._process(event)
        except Exception as exc:
            self._failed += 1
            logger.error(
                "incoming message failed",
                extra={"error": type(exc).__name__, "failed": self._failed},
            )

    async def _process(self, event: IncomingEvent) -> None:
        reason = event_rejection(event)
        if reason is not None:
            self._reject(reason)
            return
        sender = await event.get_sender()
        if sender is None:
            self._reject(Rejection.NO_SENDER)
            return
        reason = sender_rejection(sender)
        if reason is not None:
            self._reject(reason)
            return
        await self._handle_with_retry(to_incoming_message(event, sender))
        self._handled += 1
        logger.debug("incoming message processed", extra={"handled": self._handled})

    async def _handle_with_retry(self, incoming: IncomingMessage) -> None:
        for attempt, delay in enumerate(INTAKE_RETRY_DELAYS_SECONDS, start=1):
            try:
                await self._intake.handle(incoming)
            except Exception as exc:
                logger.warning(
                    "intake attempt failed, retrying",
                    extra={"error": type(exc).__name__, "attempt": attempt, "delay": delay},
                )
                await self._sleep(delay)
            else:
                return
        await self._intake.handle(incoming)

    def _reject(self, reason: Rejection) -> None:
        self._rejected[reason] += 1
        logger.debug(
            "incoming event rejected",
            extra={"reason": reason.value, "rejected": self.rejected},
        )
