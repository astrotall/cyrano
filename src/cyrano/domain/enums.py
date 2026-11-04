"""Contact statuses, message directions, LLM vocabulary, dialogue roles and outbox vocabulary."""

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


class TurnRole(StrEnum):
    ME = "me"
    THEM = "them"


class OutboxState(StrEnum):
    SCHEDULED = "scheduled"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutboxSource(StrEnum):
    AUTOPILOT = "autopilot"
    MANUAL_TEST = "manual_test"


class RejectionReason(StrEnum):
    RECIPIENT = "recipient"
    CONTENT = "content"


class LimitScope(StrEnum):
    GLOBAL = "global"
    CONTACT = "contact"


class LimitWindow(StrEnum):
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
