from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from cyrano.domain.enums import LimitScope, LimitWindow, OutboxSource, OutboxState
from cyrano.domain.models import TelegramId, UtcDatetime

MAX_OUTBOX_TEXT_LENGTH = 4096
UTF16_CODEC = "utf-16-le"
UTF16_ERROR_POLICY = "surrogatepass"
UTF16_BYTES_PER_UNIT = 2

GUARD_ALLOWED = "allowed"
REFUSED_UNKNOWN_CONTACT = "unknown_contact"
REFUSED_CONTACT_NOT_ACTIVE = "contact_not_active"
REFUSED_AUTOPILOT_DISABLED = "autopilot_disabled"
REFUSED_TEXT_EMPTY = "text_empty"
REFUSED_TEXT_TOO_LONG = "text_too_long"
CANCEL_REASON_OWNER = "cancelled_by_owner"
CANCEL_REASON_STALE = "stale"
CANCEL_REASON_SEND_REJECTED = "send_rejected"
CANCEL_REASON_MANUAL_MESSAGE = "manual_message"
CANCEL_REASON_STOP_TRIGGER = "stop_trigger"
CANCEL_REASON_SUPERSEDED = "superseded"
CANCEL_REASON_PREDECESSOR = "predecessor"
ERROR_INTERRUPTED = "interrupted"
ERROR_UNCERTAIN = "uncertain"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_OUTSIDE_HOURS = "outside_hours"
CAUSE_SEND_DEADLINE = "SendDeadlinePassed"

ROW_STALE_AFTER_SECONDS = 6 * 3600
LIMIT_NOTICE_INTERVAL_SECONDS = 3600
STALE_NOTICE_INTERVAL_SECONDS = 3600
STALE_NOTICE_CONTACT_IDS_MAX = 5
STOP_REMINDER_INTERVAL_SECONDS = 6 * 3600
LIMIT_WINDOW_SECONDS = {
    LimitWindow.MINUTE: 60,
    LimitWindow.HOUR: 3600,
    LimitWindow.DAY: 86400,
}


class ScheduledMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    id: int
    contact_id: int
    trigger_message_id: int | None
    text: str = Field(repr=False)
    source: OutboxSource
    send_at: UtcDatetime
    due_at: UtcDatetime
    state: OutboxState
    error_class: str | None
    cancel_reason: str | None
    claimed_at: UtcDatetime | None
    sent_tg_message_id: int | None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    follows_id: int | None = None


class SentMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tg_message_id: TelegramId
    sent_at: UtcDatetime


class GuardDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    reason: str = GUARD_ALLOWED


class SenderState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    paused_until: UtcDatetime | None
    pause_reason: str | None
    stopped: bool
    stop_reason: str | None
    updated_at: UtcDatetime

    def is_paused(self, now: datetime) -> bool:
        return self.paused_until is not None and self.paused_until > now


class SendLimit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: LimitScope
    window: LimitWindow
    maximum: int = Field(ge=1)


class OutgoingPeak(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contact_id: int
    count: int
