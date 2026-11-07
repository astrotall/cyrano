from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from cyrano.domain.enums import OutboxSource
from cyrano.domain.errors import EnqueueRefused
from cyrano.domain.outbox import (
    MAX_OUTBOX_TEXT_LENGTH,
    REFUSED_TEXT_EMPTY,
    REFUSED_TEXT_TOO_LONG,
    UTF16_BYTES_PER_UNIT,
    UTF16_CODEC,
    UTF16_ERROR_POLICY,
    ScheduledMessage,
)
from cyrano.services.repositories import ContactRepository, OutboxRepository
from cyrano.services.sanitize import strip_nul
from cyrano.services.send_guard import SendGuard


def utf16_units(text: str) -> int:
    return len(text.encode(UTF16_CODEC, UTF16_ERROR_POLICY)) // UTF16_BYTES_PER_UNIT


def clean_outbox_text(text: str) -> str:
    cleaned = strip_nul(text).strip()
    if not cleaned:
        raise EnqueueRefused(REFUSED_TEXT_EMPTY)
    if utf16_units(cleaned) > MAX_OUTBOX_TEXT_LENGTH:
        raise EnqueueRefused(REFUSED_TEXT_TOO_LONG)
    return cleaned


class OutboxService:
    def __init__(
        self, contacts: ContactRepository, outbox: OutboxRepository, guard: SendGuard
    ) -> None:
        self._contacts = contacts
        self._outbox = outbox
        self._guard = guard

    async def enqueue(
        self,
        contact_id: int,
        text: str,
        send_at: datetime,
        source: OutboxSource,
        trigger_message_id: int | None = None,
    ) -> ScheduledMessage:
        cleaned = clean_outbox_text(text)
        contact = await self._contacts.get_by_id(contact_id)
        decision = self._guard.check(contact, source)
        if not decision.allowed:
            raise EnqueueRefused(decision.reason)
        return await self._outbox.enqueue(contact_id, cleaned, send_at, source, trigger_message_id)

    async def enqueue_sequence(
        self,
        contact_id: int,
        texts: Sequence[str],
        send_at: datetime,
        source: OutboxSource,
        trigger_message_id: int | None = None,
    ) -> list[ScheduledMessage]:
        if not texts:
            raise EnqueueRefused(REFUSED_TEXT_EMPTY)
        cleaned = [clean_outbox_text(text) for text in texts]
        contact = await self._contacts.get_by_id(contact_id)
        decision = self._guard.check(contact, source)
        if not decision.allowed:
            raise EnqueueRefused(decision.reason)
        return await self._outbox.enqueue_sequence(
            contact_id, cleaned, send_at, source, trigger_message_id
        )
