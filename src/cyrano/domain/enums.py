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
    STOP_TRIGGER = "stop_trigger"


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


class IgnoreOutcome(StrEnum):
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


class TriggerClass(StrEnum):
    MEETING = "meeting"
    CONTACT_EXCHANGE = "contact_exchange"
    MONEY = "money"
    PHOTO = "photo"
    BOT_QUESTION = "bot_question"
    EXES = "exes"
    CRISIS = "crisis"


class TriggerSource(StrEnum):
    TEXT = "text"
    MODEL = "model"


class HandOverOutcome(StrEnum):
    HANDED_OVER = "handed_over"
    NOT_ACTIVE = "not_active"
    ALREADY_PAUSED = "already_paused"
    NOT_FOUND = "not_found"


class AutopilotStopReason(StrEnum):
    PROVIDER_AUTH = "provider_auth"
    PROVIDER_REQUEST = "provider_request"
    PERSONA_MISSING = "persona_missing"


class AutopilotPauseReason(StrEnum):
    PROVIDER_UNAVAILABLE = "provider_unavailable"


class ReplyJobOutcome(StrEnum):
    QUEUED = "queued"
    SKIPPED = "skipped"
    HANDED_OVER = "handed_over"
    STOPPED = "stopped"
    PAUSED = "paused"


class ReplyJobReason(StrEnum):
    QUEUED = "queued"
    SWITCH_OFF = "switch_off"
    AUTOPILOT_STOPPED = "autopilot_stopped"
    AUTOPILOT_PAUSED = "autopilot_paused"
    CONTACT_MISSING = "contact_missing"
    CONTACT_NOT_ACTIVE = "contact_not_active"
    TRIGGER_MISSING = "trigger_missing"
    TRIGGER_NOT_INCOMING = "trigger_not_incoming"
    TRIGGER_BEFORE_RESUME = "trigger_before_resume"
    TRIGGER_STALE = "trigger_stale"
    SUPERSEDED = "superseded"
    ANSWERED = "answered"
    ROW_EXISTS = "row_exists"
    TEXT_TRIGGER = "text_trigger"
    NEEDS_OWNER = "needs_owner"
    TRUNCATED = "truncated"
    REPLY_INVALID = "reply_invalid"
    PROMPT_TOO_LARGE = "prompt_too_large"
    MODE_UNAVAILABLE = "mode_unavailable"
    NO_MESSAGES = "no_messages"
    PERSONA_MISSING = "persona_missing"
    PROVIDER_AUTH = "provider_auth"
    PROVIDER_REQUEST = "provider_request"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PLAN_NOT_POSSIBLE = "plan_not_possible"
    ENQUEUE_REFUSED = "enqueue_refused"


class SummaryOutcome(StrEnum):
    UPDATED = "updated"
    NOTHING_TO_DO = "nothing_to_do"
    CONTACT_MISSING = "contact_missing"
    NOT_ELIGIBLE = "not_eligible"
    FAILED = "failed"


class SummaryReason(StrEnum):
    UPDATED = "updated"
    KEPT_PREVIOUS = "kept_previous"
    NO_NEW_MESSAGES = "no_new_messages"
    CONCURRENT_UPDATE = "concurrent_update"
    EMPTY_RESULT = "empty_result"
    CONTACT_MISSING = "contact_missing"
    STATUS_NOT_ELIGIBLE = "status_not_eligible"
    LLM_ERROR = "llm_error"
    INVALID_OUTPUT = "invalid_output"
    PROMPT_TOO_LARGE = "prompt_too_large"
    UNEXPECTED_ERROR = "unexpected_error"


class EarlyReadReason(StrEnum):
    DISABLED = "disabled"
    AUTOPILOT_OFF = "autopilot_off"
    AUTOPILOT_STOPPED = "autopilot_stopped"
    AUTOPILOT_PAUSED = "autopilot_paused"
    SENDER_STOPPED = "sender_stopped"
    SENDER_PAUSED = "sender_paused"
    CONTACT_MISSING = "contact_missing"
    CONTACT_NOT_ACTIVE = "contact_not_active"
    BLOCKED_WINDOW = "blocked_window"
    NOT_ONGOING = "not_ongoing"


class EarlyReadOutcome(StrEnum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    DROPPED = "dropped"
