from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cyrano.db.repositories import SqlContactRepository, SqlModeRepository
from cyrano.domain.enums import ContactStatus, OutboxSource, OutboxState
from cyrano.domain.errors import OutboxRowNotFound, OutboxStateConflict
from cyrano.domain.models import DEFAULT_MODE_NAME, Contact, ContactCreate
from cyrano.domain.outbox import ScheduledMessage, SenderState, SentMessage
from cyrano.domain.schedule import ScheduleProfile
from cyrano.domain.stop_triggers import HandOverNotice
from cyrano.services.humanization import ScheduleService

FAKE_NOW = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)
FIRST_FAKE_TG_MESSAGE_ID = 700100
EVENTUALLY_TRIES = 1000
EVENTUALLY_PAUSE = 0.01
MARK_READ_STEP = "mark_read"
SHOW_TYPING_STEP = "show_typing"
SEND_TEXT_STEP = "send_text"
FREE_TIMEZONE = "UTC"
ALWAYS_FREE_PROFILE = ScheduleProfile.model_validate(
    {
        "version": 1,
        "timezone": FREE_TIMEZONE,
        "night": {
            "start": ["00:00", "00:00"],
            "end_weekday": ["00:01", "00:01"],
            "end_weekend": ["00:01", "00:01"],
        },
        "read_pause_seconds": [0, 0],
    }
)


async def eventually(check: Callable[[], Awaitable[bool]]) -> None:
    for _ in range(EVENTUALLY_TRIES):
        if await check():
            return
        await asyncio.sleep(EVENTUALLY_PAUSE)
    raise AssertionError("the condition was not reached in time")


class FakeClock:
    def __init__(self, now: datetime = FAKE_NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass(frozen=True)
class FakeLimits:
    send_limit_minute: int = 100_000
    send_limit_hour: int = 100_000
    send_limit_day: int = 100_000
    send_limit_contact_minute: int = 100_000
    send_limit_contact_hour: int = 100_000
    send_limit_contact_day: int = 100_000


class InMemoryScheduleProfiles:
    def __init__(self, profile: ScheduleProfile | None = None) -> None:
        self.profile = profile

    async def get(self) -> ScheduleProfile | None:
        return self.profile

    async def replace(self, profile: ScheduleProfile) -> ScheduleProfile:
        self.profile = profile
        return profile


def free_schedule() -> ScheduleService:
    return ScheduleService(InMemoryScheduleProfiles(ALWAYS_FREE_PROFILE), FREE_TIMEZONE)


class ScriptedTransport:
    def __init__(
        self,
        script: Sequence[Exception] = (),
        clock: Callable[[], datetime] | None = None,
        *,
        read_errors: Sequence[BaseException] = (),
        typing_errors: Sequence[BaseException] = (),
    ) -> None:
        self.calls: list[tuple[int, str]] = []
        self.steps: list[str] = []
        self.typing_seconds: list[float] = []
        self.read_ids: list[int] = []
        self.max_active = 0
        self.delay = 0.0
        self._script = list(script)
        self._read_errors = list(read_errors)
        self._typing_errors = list(typing_errors)
        self._clock = clock
        self._active = 0

    async def mark_read(self, tg_id: int) -> None:
        self.steps.append(MARK_READ_STEP)
        self.read_ids.append(tg_id)
        if self._read_errors:
            raise self._read_errors.pop(0)

    async def show_typing(self, tg_id: int, seconds: float) -> None:
        self.steps.append(SHOW_TYPING_STEP)
        self.typing_seconds.append(seconds)
        if self._typing_errors:
            raise self._typing_errors.pop(0)

    async def send_text(self, tg_id: int, text: str) -> SentMessage:
        self.steps.append(SEND_TEXT_STEP)
        self.calls.append((tg_id, text))
        number = len(self.calls)
        self._active += 1
        self.max_active = max(self.max_active, self._active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self._script:
                raise self._script.pop(0)
            sent_at = FAKE_NOW if self._clock is None else self._clock()
            return SentMessage(tg_message_id=FIRST_FAKE_TG_MESSAGE_ID + number, sent_at=sent_at)
        finally:
            self._active -= 1


class HangingTransport:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.calls = 0

    async def mark_read(self, tg_id: int) -> None:
        return None

    async def show_typing(self, tg_id: int, seconds: float) -> None:
        return None

    async def send_text(self, tg_id: int, text: str) -> SentMessage:
        self.calls += 1
        self.started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError


class RecordingNotifier:
    def __init__(self, error: Exception | None = None) -> None:
        self.texts: list[str] = []
        self._error = error

    async def notify(self, text: str) -> None:
        self.texts.append(text)
        if self._error is not None:
            raise self._error


class RecordingManualPauseNotifier:
    def __init__(self, error: Exception | None = None) -> None:
        self.contact_ids: list[int] = []
        self._error = error

    async def notify_manual_pause(self, contact_id: int) -> None:
        self.contact_ids.append(contact_id)
        if self._error is not None:
            raise self._error


class HangingManualPauseNotifier:
    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = 0

    async def notify_manual_pause(self, contact_id: int) -> None:
        self.calls += 1
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class RecordingHandOverNotifier:
    def __init__(self, error: Exception | None = None) -> None:
        self.notices: list[HandOverNotice] = []
        self._error = error

    async def notify_hand_over(self, notice: HandOverNotice) -> None:
        self.notices.append(notice)
        if self._error is not None:
            raise self._error


class HangingHandOverNotifier:
    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = 0

    async def notify_hand_over(self, notice: HandOverNotice) -> None:
        self.calls += 1
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class HangingNotifier:
    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = 0
        self.started = asyncio.Event()

    async def notify(self, text: str) -> None:
        self.calls += 1
        self.started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class InMemoryOutbox:
    def __init__(self) -> None:
        self.rows: dict[int, ScheduledMessage] = {}
        self.completed: list[tuple[int, int, int]] = []

    def _update(self, row_id: int, **changes: object) -> ScheduledMessage:
        row = self.rows.get(row_id)
        if row is None:
            raise OutboxRowNotFound(row_id)
        updated = row.model_copy(update=changes)
        self.rows[row_id] = updated
        return updated

    def _require(self, row_id: int, *allowed: OutboxState) -> None:
        row = self.rows.get(row_id)
        if row is None:
            raise OutboxRowNotFound(row_id)
        if row.state not in allowed:
            raise OutboxStateConflict(row_id, row.state)

    async def enqueue(
        self,
        contact_id: int,
        text: str,
        send_at: datetime,
        source: OutboxSource,
        trigger_message_id: int | None = None,
    ) -> ScheduledMessage:
        row = ScheduledMessage(
            id=len(self.rows) + 1,
            contact_id=contact_id,
            trigger_message_id=trigger_message_id,
            text=text,
            source=source,
            send_at=send_at,
            due_at=send_at,
            state=OutboxState.SCHEDULED,
            error_class=None,
            cancel_reason=None,
            claimed_at=None,
            sent_tg_message_id=None,
            created_at=FAKE_NOW,
            updated_at=FAKE_NOW,
        )
        self.rows[row.id] = row
        return row

    async def get(self, row_id: int) -> ScheduledMessage | None:
        return self.rows.get(row_id)

    async def claim_due(self, now: datetime) -> ScheduledMessage | None:
        due = [
            row
            for row in self.rows.values()
            if row.state is OutboxState.SCHEDULED and row.send_at <= now
        ]
        if not due:
            return None
        first = min(due, key=lambda row: (row.send_at, row.id))
        return self._update(first.id, state=OutboxState.SENDING, claimed_at=now)

    async def reschedule(
        self, row_id: int, send_at: datetime, error_class: str | None = None
    ) -> ScheduledMessage:
        self._require(row_id, OutboxState.SENDING)
        return self._update(
            row_id,
            state=OutboxState.SCHEDULED,
            send_at=send_at,
            claimed_at=None,
            error_class=error_class,
        )

    async def complete_send(
        self, row_id: int, contact_id: int, text: str, tg_message_id: int, sent_at: datetime
    ) -> ScheduledMessage:
        self.completed.append((row_id, contact_id, tg_message_id))
        return self._update(
            row_id, state=OutboxState.SENT, sent_tg_message_id=tg_message_id, error_class=None
        )

    async def mark_cancelled(self, row_id: int, reason: str) -> ScheduledMessage:
        self._require(row_id, OutboxState.SCHEDULED, OutboxState.SENDING)
        return self._update(row_id, state=OutboxState.CANCELLED, cancel_reason=reason)

    async def cancel_scheduled(self, row_id: int, reason: str) -> ScheduledMessage:
        self._require(row_id, OutboxState.SCHEDULED)
        return self._update(row_id, state=OutboxState.CANCELLED, cancel_reason=reason)

    async def mark_failed(self, row_id: int, error_class: str) -> ScheduledMessage:
        self._require(row_id, OutboxState.SENDING)
        return self._update(row_id, state=OutboxState.FAILED, error_class=error_class)

    async def retry_failed(self, row_id: int, send_at: datetime) -> ScheduledMessage:
        self._require(row_id, OutboxState.FAILED)
        return self._update(
            row_id,
            state=OutboxState.SCHEDULED,
            send_at=send_at,
            due_at=send_at,
            claimed_at=None,
            error_class=None,
            cancel_reason=None,
        )

    async def cancel_pending_for_contact(self, contact_id: int, reason: str) -> int:
        pending = [
            row
            for row in self.rows.values()
            if row.contact_id == contact_id and row.state is OutboxState.SCHEDULED
        ]
        for row in pending:
            self._update(row.id, state=OutboxState.CANCELLED, cancel_reason=reason)
        return len(pending)

    async def has_sending_row(self, contact_id: int) -> bool:
        return any(
            row.contact_id == contact_id and row.state is OutboxState.SENDING
            for row in self.rows.values()
        )

    async def stale_sending(self, claimed_before: datetime) -> list[ScheduledMessage]:
        return [
            row
            for row in self.rows.values()
            if row.state is OutboxState.SENDING
            and row.claimed_at is not None
            and row.claimed_at <= claimed_before
        ]

    async def list_rows(self, state: OutboxState | None, limit: int) -> list[ScheduledMessage]:
        rows = [row for row in self.rows.values() if state is None or row.state is state]
        return sorted(rows, key=lambda row: row.id, reverse=True)[:limit]

    async def has_row_for_trigger(self, contact_id: int, trigger_message_id: int) -> bool:
        return any(
            (row.contact_id, row.trigger_message_id) == (contact_id, trigger_message_id)
            for row in self.rows.values()
        )

    async def count_by_state(self, source: OutboxSource | None = None) -> dict[OutboxState, int]:
        rows = [row for row in self.rows.values() if source is None or row.source is source]
        return {state: sum(row.state is state for row in rows) for state in OutboxState}


class InMemorySenderState:
    def __init__(self) -> None:
        self.state = SenderState(
            paused_until=None,
            pause_reason=None,
            stopped=False,
            stop_reason=None,
            updated_at=FAKE_NOW,
        )

    async def get(self) -> SenderState:
        return self.state

    async def pause_until(self, until: datetime, reason: str, *, now: datetime) -> bool:
        current = self.state.paused_until
        if current is None or until > current:
            self.state = self.state.model_copy(
                update={"paused_until": until, "pause_reason": reason}
            )
        return current is None or current <= now

    async def stop(self, reason: str) -> bool:
        if self.state.stopped:
            return False
        self.state = self.state.model_copy(update={"stopped": True, "stop_reason": reason})
        return True

    async def resume(self, *, keep_stop: bool = False) -> SenderState:
        before = self.state
        if not (before.stopped and keep_stop):
            self.state = before.model_copy(
                update={
                    "paused_until": None,
                    "pause_reason": None,
                    "stopped": False,
                    "stop_reason": None,
                }
            )
        return before


async def stored_contact(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    status: ContactStatus,
    *,
    tg_id: int,
    username: str | None = None,
    display_name: str | None = None,
) -> Contact:
    contact = await contacts.create(
        ContactCreate(tg_id=tg_id, username=username, display_name=display_name)
    )
    if status is ContactStatus.NEW:
        return contact
    contact = await contacts.set_status(contact.id, ContactStatus.PENDING)
    if status is ContactStatus.PENDING:
        return contact
    if status is ContactStatus.IGNORED:
        return await contacts.set_status(contact.id, ContactStatus.IGNORED)
    default = await modes.get_by_name(DEFAULT_MODE_NAME)
    assert default is not None
    contact = await contacts.set_status(contact.id, ContactStatus.ACTIVE, mode_id=default.id)
    if status is ContactStatus.PAUSED:
        return await contacts.set_status(contact.id, ContactStatus.PAUSED)
    return contact
