from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from cyrano.domain.enums import ContactStatus, MessageDirection, TriggerSource
from cyrano.domain.errors import ContactAlreadyExists
from cyrano.domain.models import (
    Contact,
    ContactCreate,
    IncomingMessage,
    MessageCreate,
    NewContactEvent,
)
from cyrano.services.autopilot import AutopilotScheduler
from cyrano.services.contact_events import ContactEventPublisher
from cyrano.services.hand_over import HandOverService
from cyrano.services.message_time import message_time
from cyrano.services.repositories import ContactRepository, MessageRepository
from cyrano.services.sanitize import sanitize_incoming
from cyrano.services.stop_triggers import detect

logger = logging.getLogger("cyrano.services.intake")


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class IntakeResult:
    contact_id: int
    message_id: int
    contact_created: bool


class IntakeService:
    def __init__(
        self,
        contacts: ContactRepository,
        messages: MessageRepository,
        events: ContactEventPublisher[NewContactEvent],
        *,
        hand_over: HandOverService | None = None,
        scheduler: AutopilotScheduler | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._contacts = contacts
        self._messages = messages
        self._events = events
        self._hand_over = hand_over
        self._scheduler = scheduler
        self._clock = clock

    async def handle(self, incoming: IncomingMessage) -> IntakeResult:
        incoming = sanitize_incoming(incoming)
        contact, created = await self._find_or_create(incoming)
        sent_at = message_time(incoming.sent_at, self._clock())
        message = await self._messages.append(
            MessageCreate(
                contact_id=contact.id,
                direction=MessageDirection.INCOMING,
                text=incoming.text,
                tg_message_id=incoming.tg_message_id,
                created_at=sent_at,
            )
        )
        await self._contacts.touch_last_message_at(contact.id, message.created_at)
        if contact.status is ContactStatus.ACTIVE:
            await self._check_stop_triggers(contact.id, incoming.text)
            if self._scheduler is not None:
                await self._scheduler.on_incoming(contact.id, message.id)
        if created:
            logger.info("contact registered", extra={"contact_id": contact.id})
            await self._events.publish(
                NewContactEvent(contact_id=contact.id, message_id=message.id)
            )
        return IntakeResult(contact_id=contact.id, message_id=message.id, contact_created=created)

    async def _check_stop_triggers(self, contact_id: int, text: str) -> None:
        if self._hand_over is None:
            return
        try:
            decision = detect(text)
            if decision.triggered:
                await self._hand_over.hand_over(contact_id, decision.classes, TriggerSource.TEXT)
        except Exception as exc:
            logger.warning(
                "stop-trigger check failed",
                extra={"contact_id": contact_id, "error": type(exc).__name__},
            )

    async def _find_or_create(self, incoming: IncomingMessage) -> tuple[Contact, bool]:
        existing = await self._contacts.get_by_tg_id(incoming.tg_id)
        if existing is not None:
            return existing, False
        try:
            created = await self._contacts.create(
                ContactCreate(
                    tg_id=incoming.tg_id,
                    username=incoming.username,
                    display_name=incoming.display_name,
                )
            )
        except ContactAlreadyExists:
            winner = await self._contacts.get_by_tg_id(incoming.tg_id)
            if winner is None:
                raise
            return winner, False
        return created, True
