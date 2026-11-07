from __future__ import annotations

import asyncio

from cyrano.domain.enums import CardOutcome, ContactStatus
from cyrano.domain.errors import InvalidStatusTransition, ModeNotFound
from cyrano.domain.models import CardActionResult, Contact
from cyrano.services.repositories import ContactRepository, ModeRepository


class CardActions:
    def __init__(self, contacts: ContactRepository, modes: ModeRepository) -> None:
        self._contacts = contacts
        self._modes = modes
        self._lock = asyncio.Lock()

    async def reply(self, contact_id: int, mode_id: int) -> CardActionResult:
        async with self._lock:
            return await self._reply(contact_id, mode_id)

    async def ignore(self, contact_id: int) -> CardActionResult:
        async with self._lock:
            return await self._ignore(contact_id)

    async def _reply(self, contact_id: int, mode_id: int) -> CardActionResult:
        contact = await self._contacts.get_by_id(contact_id)
        if contact is None:
            return CardActionResult(outcome=CardOutcome.NOT_FOUND)
        if contact.status is ContactStatus.ACTIVE:
            same_mode = contact.mode_id == mode_id
            return await self._result(
                CardOutcome.ALREADY_APPLIED if same_mode else CardOutcome.REFUSED, contact
            )
        if contact.status is not ContactStatus.PENDING:
            return await self._result(CardOutcome.REFUSED, contact)
        mode = await self._modes.get_by_id(mode_id)
        if mode is None or not mode.enabled:
            return await self._result(CardOutcome.MODE_UNAVAILABLE, contact)
        try:
            changed = await self._contacts.set_status(
                contact.id, ContactStatus.ACTIVE, mode_id=mode.id
            )
        except ModeNotFound:
            return await self._result(CardOutcome.MODE_UNAVAILABLE, contact)
        except InvalidStatusTransition:
            return await self._refused_after_change(contact.id)
        return await self._result(CardOutcome.APPLIED, changed)

    async def _ignore(self, contact_id: int) -> CardActionResult:
        contact = await self._contacts.get_by_id(contact_id)
        if contact is None:
            return CardActionResult(outcome=CardOutcome.NOT_FOUND)
        if contact.status is ContactStatus.IGNORED:
            return await self._result(CardOutcome.ALREADY_APPLIED, contact)
        if contact.status is not ContactStatus.PENDING:
            return await self._result(CardOutcome.REFUSED, contact)
        try:
            changed = await self._contacts.set_status(contact.id, ContactStatus.IGNORED)
        except InvalidStatusTransition:
            return await self._refused_after_change(contact.id)
        return await self._result(CardOutcome.APPLIED, changed)

    async def _refused_after_change(self, contact_id: int) -> CardActionResult:
        current = await self._contacts.get_by_id(contact_id)
        if current is None:
            return CardActionResult(outcome=CardOutcome.NOT_FOUND)
        return await self._result(CardOutcome.REFUSED, current)

    async def _result(self, outcome: CardOutcome, contact: Contact) -> CardActionResult:
        mode_name: str | None = None
        if contact.status is ContactStatus.ACTIVE and contact.mode_id is not None:
            mode = await self._modes.get_by_id(contact.mode_id)
            mode_name = mode.name if mode is not None else None
        return CardActionResult(outcome=outcome, status=contact.status, mode_name=mode_name)
