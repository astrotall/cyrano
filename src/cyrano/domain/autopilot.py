from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from cyrano.domain.enums import (
    AutopilotPauseReason,
    AutopilotStopReason,
    ReplyJobOutcome,
    ReplyJobReason,
)
from cyrano.domain.models import MAX_BIGINT, UtcDatetime
from cyrano.domain.outbox import ROW_STALE_AFTER_SECONDS

REPLY_STALE_AFTER_SECONDS = ROW_STALE_AFTER_SECONDS
SWEEP_INTERVAL_SECONDS = 60
SWEEP_DEBOUNCE_FACTOR = 2
JOB_MAX_TRIES = 4
RETRY_DELAYS_SECONDS = (60, 300, 900)
PROVIDER_COOLDOWN_SECONDS = 900
TRUNCATION_RETRY_FACTOR = 2
TRUNCATION_MAX_TOKENS = 1600
JOB_TIMEOUT_SECONDS = 300
WORKER_MAX_JOBS = 2
WORKER_HEALTH_CHECK_INTERVAL_SECONDS = 60
QUEUE_CONNECT_RETRIES = 1
BURST_HISTORY_MESSAGES = 20
SECONDS_PER_MINUTE = 60
ADOPTED_NEWEST_DETAIL = "adopted_newest"
REPLY_JOB_NAME = "reply_to_contact"
REPLY_JOB_ID_TEMPLATE = "reply-{contact_id}-{trigger_message_id}"


def reply_job_id(contact_id: int, trigger_message_id: int) -> str:
    return REPLY_JOB_ID_TEMPLATE.format(
        contact_id=contact_id, trigger_message_id=trigger_message_id
    )


def retry_delay(attempt: int) -> int | None:
    if attempt < 1 or attempt >= JOB_MAX_TRIES:
        return None
    return RETRY_DELAYS_SECONDS[attempt - 1]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AutopilotState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    stopped: bool
    stop_reason: AutopilotStopReason | None
    paused_until: UtcDatetime | None
    pause_reason: AutopilotPauseReason | None
    updated_at: UtcDatetime

    def is_paused(self, now: datetime) -> bool:
        return self.paused_until is not None and self.paused_until > now

    def is_running(self, now: datetime) -> bool:
        return not self.stopped and not self.is_paused(now)


class ReplyJobRequest(_Frozen):
    contact_id: int = Field(gt=0, le=MAX_BIGINT)
    trigger_message_id: int = Field(gt=0, le=MAX_BIGINT)


class ReplyJobResult(_Frozen):
    outcome: ReplyJobOutcome
    reason: ReplyJobReason
    detail: str | None = None
    row_id: int | None = None
    parts: int | None = None
    quoted: bool = False
