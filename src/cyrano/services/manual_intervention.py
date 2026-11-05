"""Pauses a contact when the owner writes to it by hand from the userbot account."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from cyrano.domain.enums import ContactStatus, ManualMessageOutcome, MessageDirection, PauseReason
from cyrano.domain.errors import InvalidStatusTransition
from cyrano.domain.models import MessageCreate, OutgoingMessage
from cyrano.domain.outbox import CANCEL_REASON_MANUAL_MESSAGE
from cyrano.services.manual_pause_notifier import ManualPauseNotifier
from cyrano.services.outbox_sender import OWNER_NOTIFY_DEADLINE
from cyrano.services.repositories import ContactRepository, MessageRepository, OutboxRepository

logger = logging.getLogger("cyrano.services.manual_intervention")

SETTLE_DELAYS_SECONDS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)


class ManualMessageDetector:
    def __init__(
        self,
        contacts: ContactRepository,
        messages: MessageRepository,
        outbox: OutboxRepository,
        notifier: ManualPauseNotifier,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        notify_deadline: float = OWNER_NOTIFY_DEADLINE,
    ) -> None:
        self._contacts = contacts
        self._messages = messages
        self._outbox = outbox
        self._notifier = notifier
        self._sleep = sleep
        self._notify_deadline = notify_deadline

    async def handle(self, outgoing: OutgoingMessage) -> ManualMessageOutcome:
        contact = await self._contacts.get_by_tg_id(outgoing.tg_id)
        if contact is None:
            return ManualMessageOutcome.UNKNOWN_PEER
        if await self._is_our_own(contact.id, outgoing.tg_message_id):
            return ManualMessageOutcome.OWN_MESSAGE
        paused = await self._pause_if_active(contact.id)
        if not paused:
            await self._store(contact.id, outgoing)
            return ManualMessageOutcome.STORED
        await self._finish_pause(contact.id, outgoing)
        return ManualMessageOutcome.PAUSED

    async def _is_our_own(self, contact_id: int, tg_message_id: int) -> bool:
        sending = await self._outbox.has_sending_row(contact_id)
        if await self._messages.exists(contact_id, tg_message_id):
            return True
        if not sending:
            return False
        for delay in SETTLE_DELAYS_SECONDS:
            await self._sleep(delay)
            if await self._messages.exists(contact_id, tg_message_id):
                return True
        return False

    async def _pause_if_active(self, contact_id: int) -> bool:
        current = await self._contacts.get_by_id(contact_id)
        if current is None or current.status is not ContactStatus.ACTIVE:
            return False
        try:
            change = await self._contacts.set_status(
                contact_id, ContactStatus.PAUSED, pause_reason=PauseReason.MANUAL_MESSAGE
            )
        except InvalidStatusTransition:
            return False
        return change.changed

    async def _finish_pause(self, contact_id: int, outgoing: OutgoingMessage) -> None:
        logger.info("a manual message paused the contact", extra={"contact_id": contact_id})
        await self._guarded("cancel", self._cancel_scheduled(contact_id), contact_id)
        await self._guarded("store", self._store(contact_id, outgoing), contact_id)
        await self._guarded("notify", self._notify(contact_id), contact_id)

    async def _cancel_scheduled(self, contact_id: int) -> None:
        await self._outbox.cancel_pending_for_contact(contact_id, CANCEL_REASON_MANUAL_MESSAGE)

    async def _store(self, contact_id: int, outgoing: OutgoingMessage) -> None:
        await self._messages.append(
            MessageCreate(
                contact_id=contact_id,
                direction=MessageDirection.OUTGOING,
                text=outgoing.text,
                tg_message_id=outgoing.tg_message_id,
                created_at=outgoing.sent_at,
            )
        )

    async def _notify(self, contact_id: int) -> None:
        async with asyncio.timeout(self._notify_deadline):
            await self._notifier.notify_manual_pause(contact_id)

    @staticmethod
    async def _guarded(step: str, work: Awaitable[None], contact_id: int) -> None:
        try:
            await work
        except Exception as exc:
            logger.warning(
                "a step after a manual pause failed",
                extra={"step": step, "contact_id": contact_id, "error": type(exc).__name__},
            )
