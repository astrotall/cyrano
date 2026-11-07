from __future__ import annotations

from typing import Protocol

from cyrano.domain.stop_triggers import HandOverNotice


class HandOverNotifier(Protocol):
    async def notify_hand_over(self, notice: HandOverNotice) -> None: ...
