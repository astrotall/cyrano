"""The interface through which the app asks the worker to answer a message."""

from __future__ import annotations

from typing import Protocol


class ReplyJobQueue(Protocol):
    async def enqueue(
        self, contact_id: int, trigger_message_id: int, defer_seconds: int
    ) -> None: ...
