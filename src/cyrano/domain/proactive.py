from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from cyrano.domain.autopilot import AutopilotState
from cyrano.domain.enums import ProactiveJobOutcome, ProactiveJobReason
from cyrano.domain.models import MAX_BIGINT, Contact, Message
from cyrano.domain.outbox import SenderState
from cyrano.domain.schedule import UnavailableSoon

SECONDS_PER_HOUR = 3600
HOURS_PER_DAY = 24
PROACTIVE_SWEEP_LIMIT = 10
PROACTIVE_DAILY_WINDOW_SECONDS = 24 * SECONDS_PER_HOUR
MODEL_SKIP_POSTPONE_SECONDS = 12 * SECONDS_PER_HOUR
FAILURE_POSTPONE_SECONDS = SECONDS_PER_HOUR
PROACTIVE_JOB_NAME = "proactive_to_contact"
PROACTIVE_JOB_ID_TEMPLATE = "proactive-{contact_id}-{last_message_id}"
OUTSIDE_WINDOW_KIND = "proactive_window"


def proactive_job_id(contact_id: int, last_message_id: int) -> str:
    return PROACTIVE_JOB_ID_TEMPLATE.format(contact_id=contact_id, last_message_id=last_message_id)


@dataclass(frozen=True, slots=True)
class DialogueTail:
    last: Message | None
    last_incoming: Message | None


@dataclass(frozen=True, slots=True)
class ProactiveSnapshot:
    now: datetime
    autopilot: AutopilotState
    sender: SenderState
    contact: Contact | None
    tail: DialogueTail
    silence_rows: int
    newest_row_due_at: datetime | None
    daily_rows: int
    unavailable: UnavailableSoon | None = None


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProactiveDue(_Frozen):
    contact_id: int
    last_message_id: int


class ProactiveJobRequest(_Frozen):
    contact_id: int = Field(gt=0, le=MAX_BIGINT)
    last_message_id: int = Field(gt=0, le=MAX_BIGINT)


class ProactiveJobResult(_Frozen):
    outcome: ProactiveJobOutcome
    reason: ProactiveJobReason
    detail: str | None = None
    row_id: int | None = None
    parts: int | None = None
