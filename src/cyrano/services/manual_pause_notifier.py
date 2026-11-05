"""The interface through which the owner learns that a contact was paused by a manual message."""

from __future__ import annotations

from typing import Protocol


class ManualPauseNotifier(Protocol):
    async def notify_manual_pause(self, contact_id: int) -> None: ...
