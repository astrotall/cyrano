"""The owner's decision on a paused contact: resume automatic replies or ignore it."""

from __future__ import annotations

import asyncio

from cyrano.domain.enums import ContactStatus, IgnoreOutcome, ResumeOutcome
from cyrano.domain.errors import InvalidStatusTransition
from cyrano.services.repositories import ContactRepository


class PauseActions:
    def __init__(self, contacts: ContactRepository) -> None:
        self._contacts = contacts
        self._lock = asyncio.Lock()

    async def resume(self, contact_id: int) -> ResumeOutcome:
        async with self._lock:
            return await self._resume(contact_id)

    async def _resume(self, contact_id: int) -> ResumeOutcome:
        contact = await self._contacts.get_by_id(contact_id)
        if contact is None:
            return ResumeOutcome.NOT_FOUND
        if contact.status is ContactStatus.ACTIVE:
            return ResumeOutcome.ALREADY_APPLIED
        if contact.status is not ContactStatus.PAUSED or contact.mode_id is None:
            return ResumeOutcome.REFUSED
        try:
            await self._contacts.set_status(contact.id, ContactStatus.ACTIVE)
        except InvalidStatusTransition:
            return ResumeOutcome.REFUSED
        return ResumeOutcome.APPLIED

    async def ignore(self, contact_id: int) -> IgnoreOutcome:
        async with self._lock:
            return await self._ignore(contact_id)

    async def _ignore(self, contact_id: int) -> IgnoreOutcome:
        contact = await self._contacts.get_by_id(contact_id)
        if contact is None:
            return IgnoreOutcome.NOT_FOUND
        if contact.status is ContactStatus.IGNORED:
            return IgnoreOutcome.ALREADY_APPLIED
        if contact.status is not ContactStatus.PAUSED:
            return IgnoreOutcome.REFUSED
        try:
            await self._contacts.set_status(contact.id, ContactStatus.IGNORED)
        except InvalidStatusTransition:
            return IgnoreOutcome.REFUSED
        return IgnoreOutcome.APPLIED
