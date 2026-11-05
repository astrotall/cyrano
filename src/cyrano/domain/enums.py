"""Contact statuses, message directions, LLM, dialogue, outbox and schedule vocabulary."""

from __future__ import annotations

from enum import StrEnum


class ContactStatus(StrEnum):
    NEW = "new"
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    IGNORED = "ignored"


class PauseReason(StrEnum):
    MANUAL_MESSAGE = "manual_message"
    SEND_REJECTED = "send_rejected"
    OWNER = "owner"


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


class ResumeOutcome(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    REFUSED = "refused"
    NOT_FOUND = "not_found"


class ManualMessageOutcome(StrEnum):
    UNKNOWN_PEER = "unknown_peer"
    OWN_MESSAGE = "own_message"
    STORED = "stored"
    PAUSED = "paused"


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


class Weekday(StrEnum):
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"


class BlockKind(StrEnum):
    NIGHT = "night"
    BUSY = "busy"


class PlanReason(StrEnum):
    ONGOING = "ongoing"
    DELAYED = "delayed"
    POSTPONED_NIGHT = "postponed_night"
    POSTPONED_BUSY = "postponed_busy"
    EXHAUSTED = "exhausted"
