"""Contact statuses, message directions and LLM vocabulary."""

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


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class LLMStep(StrEnum):
    REPLY = "reply"
    SUMMARY = "summary"
