from __future__ import annotations

import asyncio

from cyrano.domain.enums import ContactGender, GenderOutcome
from cyrano.domain.errors import ContactNotFound
from cyrano.domain.models import CARD_MESSAGES_LIMIT, Contact, GenderActionResult
from cyrano.services.card_builder import build_card
from cyrano.services.gender import GenderResolver
from cyrano.services.repositories import ContactRepository, MessageRepository, ModeRepository


class GenderActions:
    def __init__(
        self,
        contacts: ContactRepository,
        messages: MessageRepository,
        modes: ModeRepository,
        gender: GenderResolver,
    ) -> None:
        self._contacts = contacts
        self._messages = messages
        self._modes = modes
        self._gender = gender
        self._lock = asyncio.Lock()

    async def set_gender(self, contact_id: int, gender: ContactGender) -> GenderActionResult:
        async with self._lock:
            return await self._set_gender(contact_id, gender)

    async def _set_gender(self, contact_id: int, gender: ContactGender) -> GenderActionResult:
        contact = await self._contacts.get_by_id(contact_id)
        if contact is None:
            return GenderActionResult(outcome=GenderOutcome.NOT_FOUND)
        if contact.gender is gender:
            return await self._result(GenderOutcome.ALREADY_APPLIED, contact)
        try:
            changed = await self._contacts.set_gender(contact.id, gender)
        except ContactNotFound:
            return GenderActionResult(outcome=GenderOutcome.NOT_FOUND)
        return await self._result(GenderOutcome.APPLIED, changed)

    async def _result(self, outcome: GenderOutcome, contact: Contact) -> GenderActionResult:
        resolved = self._gender.resolve(contact)
        history = await self._messages.list_last(contact.id, CARD_MESSAGES_LIMIT)
        modes = await self._modes.list_enabled()
        return GenderActionResult(
            outcome=outcome, gender=resolved, card=build_card(contact, history, modes, resolved)
        )
