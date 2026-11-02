"""Registration of incoming private messages."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cyrano.domain.enums import MessageDirection
from cyrano.domain.errors import ContactAlreadyExists
from cyrano.domain.models import (
    Contact,
    ContactCreate,
    IncomingMessage,
    MessageCreate,
    NewContactEvent,
)
from cyrano.services.contact_events import ContactEventPublisher
from cyrano.services.repositories import ContactRepository, MessageRepository
from cyrano.services.sanitize import sanitize_incoming

logger = logging.getLogger("cyrano.services.intake")


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
    ) -> None:
        self._contacts = contacts
        self._messages = messages
        self._events = events

    async def handle(self, incoming: IncomingMessage) -> IntakeResult:
        incoming = sanitize_incoming(incoming)
        contact, created = await self._find_or_create(incoming)
        message = await self._messages.append(
            MessageCreate(
                contact_id=contact.id,
                direction=MessageDirection.INCOMING,
                text=incoming.text,
                tg_message_id=incoming.tg_message_id,
            )
        )
        await self._contacts.touch_last_message_at(contact.id, incoming.sent_at)
        if created:
            logger.info("contact registered", extra={"contact_id": contact.id})
            await self._events.publish(
                NewContactEvent(contact_id=contact.id, message_id=message.id)
            )
        return IntakeResult(contact_id=contact.id, message_id=message.id, contact_created=created)

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
