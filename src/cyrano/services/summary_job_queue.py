from __future__ import annotations

from typing import Protocol


class SummaryJobQueue(Protocol):
    async def enqueue(self, contact_id: int, up_to_message_id: int) -> None: ...
