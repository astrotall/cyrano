"""The interface through which a card reaches the owner."""

from __future__ import annotations

from typing import Protocol

from cyrano.domain.models import ContactCard


class CardSender(Protocol):
    async def send_card(self, card: ContactCard) -> bool: ...
