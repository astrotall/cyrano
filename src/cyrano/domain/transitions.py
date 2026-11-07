from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from cyrano.domain.enums import ContactStatus
from cyrano.domain.errors import ActivationWithoutMode, InvalidStatusTransition

ALLOWED_TRANSITIONS: Mapping[ContactStatus, frozenset[ContactStatus]] = MappingProxyType(
    {
        ContactStatus.NEW: frozenset({ContactStatus.PENDING}),
        ContactStatus.PENDING: frozenset({ContactStatus.ACTIVE, ContactStatus.IGNORED}),
        ContactStatus.ACTIVE: frozenset({ContactStatus.PAUSED, ContactStatus.IGNORED}),
        ContactStatus.PAUSED: frozenset({ContactStatus.ACTIVE, ContactStatus.IGNORED}),
        ContactStatus.IGNORED: frozenset(),
    }
)


def transition_needed(current: ContactStatus, requested: ContactStatus, *, has_mode: bool) -> bool:
    if requested is current:
        return False
    if requested not in ALLOWED_TRANSITIONS[current]:
        raise InvalidStatusTransition(current, requested)
    if requested is ContactStatus.ACTIVE and not has_mode:
        raise ActivationWithoutMode(current)
    return True
