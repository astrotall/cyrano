"""The interface through which the services tell the owner something."""

from __future__ import annotations

from typing import Protocol


class OwnerNotifier(Protocol):
    async def notify(self, text: str) -> None: ...
