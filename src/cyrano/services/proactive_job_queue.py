from __future__ import annotations

from typing import Protocol


class ProactiveJobQueue(Protocol):
    async def enqueue(self, contact_id: int, last_message_id: int) -> None: ...
