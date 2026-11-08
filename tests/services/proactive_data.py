from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cyrano.domain.autopilot import AutopilotState
from cyrano.domain.enums import ContactStatus, MessageDirection
from cyrano.domain.models import Contact, Message
from cyrano.domain.outbox import SenderState
from cyrano.domain.proactive import DialogueTail, ProactiveSnapshot

NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
CONTACT_ID = 41
INCOMING_ID = 501
OUTGOING_ID = 502
HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class Rules:
    proactive_enabled: bool = True
    autopilot_enabled: bool = True
    proactive_after_hours: int = 6
    proactive_max_silence_days: int = 3
    proactive_max_per_silence: int = 2
    proactive_min_interval_hours: int = 24
    proactive_daily_limit: int = 20


def contact(
    status: ContactStatus = ContactStatus.ACTIVE,
    *,
    reply_after: datetime | None = None,
    not_before: datetime | None = None,
) -> Contact:
    return Contact(
        id=CONTACT_ID,
        tg_id=987654321012,
        username=None,
        display_name=None,
        status=status,
        pause_reason=None,
        mode_id=None,
        profile={},
        summary={},
        last_message_at=None,
        reply_after=reply_after,
        proactive_not_before=not_before,
        created_at=NOW - 30 * HOUR,
        updated_at=NOW - 30 * HOUR,
    )


def message(message_id: int, direction: MessageDirection, at: datetime) -> Message:
    return Message(
        id=message_id,
        contact_id=CONTACT_ID,
        direction=direction,
        text="invented-proactive-text-zq",
        tg_message_id=message_id + 1000,
        created_at=at,
    )


def autopilot_state(
    *, stopped: bool = False, paused_until: datetime | None = None
) -> AutopilotState:
    return AutopilotState(
        stopped=stopped,
        stop_reason=None,
        paused_until=paused_until,
        pause_reason=None,
        updated_at=NOW,
    )


def sender_state(*, stopped: bool = False, paused_until: datetime | None = None) -> SenderState:
    return SenderState(
        paused_until=paused_until,
        pause_reason=None,
        stopped=stopped,
        stop_reason=None,
        updated_at=NOW,
    )


def eligible_snapshot(*, silence: timedelta = 7 * HOUR) -> ProactiveSnapshot:
    last = message(OUTGOING_ID, MessageDirection.OUTGOING, NOW - silence)
    incoming = message(INCOMING_ID, MessageDirection.INCOMING, NOW - silence - HOUR)
    return ProactiveSnapshot(
        now=NOW,
        autopilot=autopilot_state(),
        sender=sender_state(),
        contact=contact(),
        tail=DialogueTail(last=last, last_incoming=incoming),
        silence_rows=0,
        newest_row_due_at=None,
        daily_rows=0,
    )
