from __future__ import annotations

from typing import Protocol


class ManualPauseNotifier(Protocol):
    async def notify_manual_pause(self, contact_id: int) -> None: ...
