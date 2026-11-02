"""Storage interfaces owned by services and satisfied by db."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from cyrano.domain.enums import ContactStatus
from cyrano.domain.models import (
    Contact,
    ContactCreate,
    ContactUpdate,
    Message,
    MessageCreate,
    Mode,
    ModeCreate,
)


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
