from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Iterable

from cyrano.domain.enums import (
    ContactStatus,
    HandOverOutcome,
    MediaKind,
    PauseReason,
    TriggerClass,
    TriggerSource,
)
from cyrano.domain.errors import ContactNotFound, InvalidStatusTransition
from cyrano.domain.models import Contact, Message
from cyrano.domain.outbox import CANCEL_REASON_STOP_TRIGGER
from cyrano.domain.stop_triggers import (
    HAND_OVER_HISTORY_LINES,
    HAND_OVER_LINE_MAX_CHARS,
    HAND_OVER_NAME_MAX_CHARS,
    DialogueLine,
    HandOverNotice,
    StopDecision,
    truncate_line,
)
from cyrano.prompts.render import media_text
from cyrano.services.hand_over_notifier import HandOverNotifier
from cyrano.services.outbox_sender import OWNER_NOTIFY_DEADLINE
from cyrano.services.repositories import ContactRepository, MessageRepository, OutboxRepository

logger = logging.getLogger("cyrano.services.hand_over")


def _line(message: Message) -> DialogueLine:
    return DialogueLine(
        direction=message.direction,
        text=truncate_line(media_text(message.text, message.media_kind), HAND_OVER_LINE_MAX_CHARS),
    )


def _name(contact: Contact) -> str | None:
    if not contact.display_name:
        return None
    return truncate_line(contact.display_name, HAND_OVER_NAME_MAX_CHARS)


class HandOverService:
    def __init__(
        self,
        contacts: ContactRepository,
        messages: MessageRepository,
        outbox: OutboxRepository,
        notifier: HandOverNotifier,
        *,
        notify_deadline: float = OWNER_NOTIFY_DEADLINE,
    ) -> None:
        self._contacts = contacts
        self._messages = messages
        self._outbox = outbox
        self._notifier = notifier
        self._notify_deadline = notify_deadline

    async def hand_over(
        self,
        contact_id: int,
        classes: Iterable[TriggerClass],
        source: TriggerSource,
        media_kind: MediaKind | None = None,
    ) -> HandOverOutcome:
        contact = await self._contacts.get_by_id(contact_id)
        if contact is None:
            return HandOverOutcome.NOT_FOUND
        if contact.status is ContactStatus.PAUSED:
            return HandOverOutcome.ALREADY_PAUSED
        if contact.status is not ContactStatus.ACTIVE:
            return HandOverOutcome.NOT_ACTIVE
        try:
            change = await self._contacts.set_status(
                contact_id, ContactStatus.PAUSED, pause_reason=PauseReason.STOP_TRIGGER
            )
        except ContactNotFound:
            return HandOverOutcome.NOT_FOUND
        except InvalidStatusTransition:
            return HandOverOutcome.NOT_ACTIVE
        if not change.changed:
            return HandOverOutcome.ALREADY_PAUSED
        ordered = StopDecision(classes=frozenset(classes)).ordered()
        logger.info(
            "a stop-trigger handed the contact over",
            extra={
                "contact_id": contact_id,
                "classes": [trigger.value for trigger in ordered],
                "source": source.value,
            },
        )
        await self._guarded("cancel", self._cancel_scheduled(contact_id), contact_id)
        lines = await self._recent_lines(contact_id)
        notice = HandOverNotice(
            contact_id=contact_id,
            name=_name(contact),
            classes=ordered,
            source=source,
            media_kind=media_kind,
            lines=lines,
        )
        await self._guarded("notify", self._notify(notice), contact_id)
        return HandOverOutcome.HANDED_OVER

    async def _cancel_scheduled(self, contact_id: int) -> None:
        await self._outbox.cancel_pending_for_contact(contact_id, CANCEL_REASON_STOP_TRIGGER)

    async def _recent_lines(self, contact_id: int) -> tuple[DialogueLine, ...]:
        try:
            history = await self._messages.list_last(contact_id, HAND_OVER_HISTORY_LINES)
        except Exception as exc:
            logger.warning(
                "a step after a hand-over failed",
                extra={"step": "history", "contact_id": contact_id, "error": type(exc).__name__},
            )
            return ()
        return tuple(_line(message) for message in history)

    async def _notify(self, notice: HandOverNotice) -> None:
        async with asyncio.timeout(self._notify_deadline):
            await self._notifier.notify_hand_over(notice)

    @staticmethod
    async def _guarded(step: str, work: Awaitable[None], contact_id: int) -> None:
        try:
            await work
        except Exception as exc:
            logger.warning(
                "a step after a hand-over failed",
                extra={"step": step, "contact_id": contact_id, "error": type(exc).__name__},
            )
