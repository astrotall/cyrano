"""Contact statuses and message directions."""

from __future__ import annotations

from enum import StrEnum


class ContactStatus(StrEnum):
    NEW = "new"
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    IGNORED = "ignored"


class MessageDirection(StrEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"


class LeomatchNameSource(StrEnum):
    CARD = "card"
    MATCH = "match"


class CardOutcome(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    REFUSED = "refused"
    NOT_FOUND = "not_found"
    MODE_UNAVAILABLE = "mode_unavailable"
