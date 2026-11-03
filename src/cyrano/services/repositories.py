"""Storage interfaces owned by services and satisfied by db."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from cyrano.domain.enums import ContactStatus, OutboxSource, OutboxState
from cyrano.domain.models import (
    Contact,
    ContactCreate,
    ContactUpdate,
    Message,
    MessageCreate,
    Mode,
    ModeCreate,
)
from cyrano.domain.outbox import ScheduledMessage
from cyrano.domain.style import ExamplesAddResult, Persona, StyleExample, StyleExampleCreate


class ContactRepository(Protocol):
    async def get_by_id(self, contact_id: int) -> Contact | None: ...

    async def get_by_tg_id(self, tg_id: int) -> Contact | None: ...

    async def create(self, data: ContactCreate) -> Contact: ...

    async def set_status(
        self, contact_id: int, status: ContactStatus, *, mode_id: int | None = None
    ) -> Contact: ...

    async def set_mode(self, contact_id: int, mode_id: int) -> Contact: ...

    async def update(self, contact_id: int, data: ContactUpdate) -> Contact: ...

    async def update_identity(
        self, contact_id: int, username: str | None, display_name: str | None
    ) -> Contact: ...

    async def touch_last_message_at(self, contact_id: int, at: datetime) -> Contact: ...

    async def list_by_status(self, status: ContactStatus) -> list[Contact]: ...


class MessageRepository(Protocol):
    async def append(self, data: MessageCreate) -> Message: ...

    async def list_last(self, contact_id: int, limit: int) -> list[Message]: ...


class ModeRepository(Protocol):
    async def get_by_id(self, mode_id: int) -> Mode | None: ...

    async def get_by_name(self, name: str) -> Mode | None: ...

    async def list_enabled(self) -> list[Mode]: ...

    async def create(self, data: ModeCreate) -> Mode: ...

    async def resolve_by_id(self, mode_id: int | None) -> Mode: ...

    async def resolve_by_name(self, name: str) -> Mode: ...


class PersonaRepository(Protocol):
    async def get(self) -> Persona | None: ...

    async def replace(self, text: str) -> Persona: ...


class StyleExampleRepository(Protocol):
    async def add_many(
        self, examples: Sequence[StyleExampleCreate], *, replace: bool = False
    ) -> ExamplesAddResult: ...

    async def list_all(self) -> list[StyleExample]: ...

    async def count(self) -> int: ...

    async def delete_all(self) -> int: ...


class OutboxRepository(Protocol):
    async def enqueue(
        self,
        contact_id: int,
        text: str,
        send_at: datetime,
        source: OutboxSource,
        trigger_message_id: int | None = None,
    ) -> ScheduledMessage: ...

    async def get(self, row_id: int) -> ScheduledMessage | None: ...

    async def claim_due(self, now: datetime) -> ScheduledMessage | None: ...

    async def reschedule(
        self, row_id: int, send_at: datetime, error_class: str | None = None
    ) -> ScheduledMessage: ...

    async def complete_send(
        self, row_id: int, contact_id: int, text: str, tg_message_id: int, sent_at: datetime
    ) -> ScheduledMessage: ...

    async def mark_cancelled(self, row_id: int, reason: str) -> ScheduledMessage: ...

    async def cancel_scheduled(self, row_id: int, reason: str) -> ScheduledMessage: ...

    async def mark_failed(self, row_id: int, error_class: str) -> ScheduledMessage: ...

    async def retry_failed(self, row_id: int, send_at: datetime) -> ScheduledMessage: ...

    async def cancel_pending_for_contact(self, contact_id: int, reason: str) -> int: ...

    async def stale_sending(self, claimed_before: datetime) -> list[ScheduledMessage]: ...

    async def list_rows(self, state: OutboxState | None, limit: int) -> list[ScheduledMessage]: ...
