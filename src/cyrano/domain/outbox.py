"""The outbox row, the transport result, the guard decision and their vocabulary."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from cyrano.domain.enums import OutboxSource, OutboxState
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
ERROR_INTERRUPTED = "interrupted"
ERROR_UNCERTAIN = "uncertain"
CAUSE_SEND_DEADLINE = "SendDeadlinePassed"


class ScheduledMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    id: int
    contact_id: int
    trigger_message_id: int | None
    text: str = Field(repr=False)
    source: OutboxSource
    send_at: UtcDatetime
    state: OutboxState
    error_class: str | None
    cancel_reason: str | None
    claimed_at: UtcDatetime | None
    sent_tg_message_id: int | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class SentMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tg_message_id: TelegramId
    sent_at: UtcDatetime


class GuardDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    reason: str = GUARD_ALLOWED
