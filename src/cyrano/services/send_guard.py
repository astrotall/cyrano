from __future__ import annotations

from typing import Protocol

from cyrano.domain.enums import ContactStatus, OutboxSource
from cyrano.domain.models import Contact
from cyrano.domain.outbox import (
    REFUSED_AUTOPILOT_DISABLED,
    REFUSED_CONTACT_NOT_ACTIVE,
    REFUSED_UNKNOWN_CONTACT,
    GuardDecision,
)


class GuardSettings(Protocol):
    @property
    def autopilot_enabled(self) -> bool: ...


def check(
    contact: Contact | None, row_source: OutboxSource, settings: GuardSettings
) -> GuardDecision:
    if contact is None:
        return GuardDecision(allowed=False, reason=REFUSED_UNKNOWN_CONTACT)
    if contact.status is not ContactStatus.ACTIVE:
        return GuardDecision(allowed=False, reason=REFUSED_CONTACT_NOT_ACTIVE)
    if row_source is OutboxSource.AUTOPILOT and not settings.autopilot_enabled:
        return GuardDecision(allowed=False, reason=REFUSED_AUTOPILOT_DISABLED)
    return GuardDecision(allowed=True)


class SendGuard:
    def __init__(self, settings: GuardSettings) -> None:
        self._settings = settings

    def check(self, contact: Contact | None, row_source: OutboxSource) -> GuardDecision:
        return check(contact, row_source, self._settings)
