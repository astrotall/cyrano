from __future__ import annotations

from typing import Protocol

from cyrano.domain.outbox import SentMessage


class MessageTransport(Protocol):
    async def send_text(
        self, tg_id: int, text: str, reply_to: int | None = None
    ) -> SentMessage: ...

    async def show_typing(self, tg_id: int, seconds: float) -> None: ...

    async def mark_read(self, tg_id: int) -> None: ...
