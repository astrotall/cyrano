"""The interface through which a message reaches a contact. Only the sender may call it."""

from __future__ import annotations

from typing import Protocol

from cyrano.domain.outbox import SentMessage


class MessageTransport(Protocol):
    async def send_text(self, tg_id: int, text: str) -> SentMessage: ...
