"""SQLAlchemy repositories. Every method is one transaction and returns domain models."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import delete, exists, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from cyrano.db.models import (
    AUTOPILOT_STATE_ROW_ID,
    PERSONA_ROW_ID,
    SCHEDULE_PROFILE_ROW_ID,
    SENDER_STATE_ROW_ID,
    AutopilotStateRow,
    ContactRow,
    MessageRow,
    ModeRow,
    PersonaRow,
    ScheduledMessageRow,
    ScheduleProfileRow,
    SenderStateRow,
    StyleExampleRow,
)
from cyrano.db.session import SessionFactory
from cyrano.domain.autopilot import AutopilotState
from cyrano.domain.enums import (
    AutopilotPauseReason,
    AutopilotStopReason,
    ContactStatus,
    MessageDirection,
    OutboxSource,
    OutboxState,
    PauseReason,
)
from cyrano.domain.errors import (
    AutopilotStateMissing,
    ContactAlreadyExists,
    ContactNotFound,
    DefaultModeUnavailable,
    ModeAlreadyExists,
    ModeNotFound,
    OutboxRowInvalid,
    OutboxRowNotFound,
    OutboxStateConflict,
    PauseReasonRefused,
    ReplyAfterRefused,
    SenderStateMissing,
    StoredScheduleInvalid,
)
from cyrano.domain.models import (
    DEFAULT_MODE_NAME,
    Contact,
    ContactCreate,
    ContactUpdate,
    Message,
    MessageCreate,
    Mode,
    ModeCreate,
    StatusChange,
)
from cyrano.domain.outbox import OutgoingPeak, ScheduledMessage, SenderState
from cyrano.domain.schedule import ScheduleProfile
from cyrano.domain.style import ExamplesAddResult, Persona, StyleExample, StyleExampleCreate
from cyrano.domain.transitions import transition_needed


async def _locked_contact(session: AsyncSession, contact_id: int) -> ContactRow:
    row = await session.get(ContactRow, contact_id, with_for_update=True)
    if row is None:
        raise ContactNotFound(contact_id)
    return row


async def _ensure_mode_exists(session: AsyncSession, mode_id: int) -> None:
    if await session.get(ModeRow, mode_id) is None:
        raise ModeNotFound(mode_id)


def _status_change(row: ContactRow, *, changed: bool) -> StatusChange:
    contact = Contact.model_validate(row)
    return StatusChange.model_validate({**contact.model_dump(), "changed": changed})


class SqlContactRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get_by_id(self, contact_id: int) -> Contact | None:
        async with self._session_factory() as session:
            row = await session.get(ContactRow, contact_id)
            return None if row is None else Contact.model_validate(row)

    async def get_by_tg_id(self, tg_id: int) -> Contact | None:
        async with self._session_factory() as session:
            row = await session.scalar(select(ContactRow).where(ContactRow.tg_id == tg_id))
            return None if row is None else Contact.model_validate(row)

    async def create(self, data: ContactCreate) -> Contact:
        row = ContactRow(
            tg_id=data.tg_id,
            username=data.username,
            display_name=data.display_name,
            status=ContactStatus.NEW,
            profile=data.profile,
            summary={},
        )
        try:
            async with self._session_factory.begin() as session:
                session.add(row)
                await session.flush()
                return Contact.model_validate(row)
        except IntegrityError:
            raise ContactAlreadyExists from None

    async def set_status(
        self,
        contact_id: int,
        status: ContactStatus,
        *,
        mode_id: int | None = None,
        pause_reason: PauseReason | None = None,
        reply_after: datetime | None = None,
    ) -> StatusChange:
        if pause_reason is not None and status is not ContactStatus.PAUSED:
            raise PauseReasonRefused(status)
        if reply_after is not None and status is not ContactStatus.ACTIVE:
            raise ReplyAfterRefused(status)
        if reply_after is not None:
            _require_aware(reply_after)
        async with self._session_factory.begin() as session:
            row = await _locked_contact(session, contact_id)
            mode_on_activation = mode_id if status is ContactStatus.ACTIVE else None
            has_mode = mode_on_activation is not None or row.mode_id is not None
            changed = transition_needed(row.status, status, has_mode=has_mode)
            if changed:
                if mode_on_activation is not None:
                    await _ensure_mode_exists(session, mode_on_activation)
                    row.mode_id = mode_on_activation
                row.status = status
                row.pause_reason = pause_reason
                if reply_after is not None:
                    row.reply_after = reply_after
                await session.flush()
            return _status_change(row, changed=changed)

    async def set_mode(self, contact_id: int, mode_id: int) -> Contact:
        async with self._session_factory.begin() as session:
            row = await _locked_contact(session, contact_id)
            await _ensure_mode_exists(session, mode_id)
            row.mode_id = mode_id
            await session.flush()
            return Contact.model_validate(row)

    async def update(self, contact_id: int, data: ContactUpdate) -> Contact:
        async with self._session_factory.begin() as session:
            row = await _locked_contact(session, contact_id)
            if data.profile is not None:
                row.profile = data.profile
            if data.summary is not None:
                row.summary = data.summary
            await session.flush()
            return Contact.model_validate(row)

    async def update_identity(
        self, contact_id: int, username: str | None, display_name: str | None
    ) -> Contact:
        async with self._session_factory.begin() as session:
            row = await _locked_contact(session, contact_id)
            row.username = username
            row.display_name = display_name
            await session.flush()
            return Contact.model_validate(row)

    async def touch_last_message_at(self, contact_id: int, at: datetime) -> Contact:
        if at.utcoffset() is None:
            msg = "last_message_at must be timezone-aware"
            raise ValueError(msg)
        async with self._session_factory.begin() as session:
            row = await _locked_contact(session, contact_id)
            if row.last_message_at is None or at > row.last_message_at:
                row.last_message_at = at
                await session.flush()
            return Contact.model_validate(row)

    async def list_by_status(self, status: ContactStatus) -> list[Contact]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ContactRow).where(ContactRow.status == status).order_by(ContactRow.id)
            )
            return [Contact.model_validate(row) for row in rows]


class SqlMessageRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def append(self, data: MessageCreate) -> Message:
        values: dict[str, object] = {
            "contact_id": data.contact_id,
            "direction": data.direction,
            "text": data.text,
            "tg_message_id": data.tg_message_id,
        }
        if data.created_at is not None:
            values["created_at"] = data.created_at
        insert_new = (
            insert(MessageRow)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[MessageRow.contact_id, MessageRow.tg_message_id]
            )
            .returning(MessageRow)
        )
        select_existing = select(MessageRow).where(
            MessageRow.contact_id == data.contact_id,
            MessageRow.tg_message_id == data.tg_message_id,
        )
        try:
            async with self._session_factory.begin() as session:
                row = await session.scalar(insert_new)
                if row is None:
                    row = (await session.scalars(select_existing)).one()
                return Message.model_validate(row)
        except IntegrityError:
            raise ContactNotFound(data.contact_id) from None

    async def get(self, message_id: int) -> Message | None:
        async with self._session_factory() as session:
            row = await session.get(MessageRow, message_id)
            return None if row is None else Message.model_validate(row)

    async def exists(self, contact_id: int, tg_message_id: int) -> bool:
        statement = select(
            select(MessageRow.id)
            .where(MessageRow.contact_id == contact_id, MessageRow.tg_message_id == tg_message_id)
            .exists()
        )
        async with self._session_factory() as session:
            return bool(await session.scalar(statement))

    async def list_last(self, contact_id: int, limit: int) -> list[Message]:
        if limit <= 0:
            return []
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(MessageRow)
                .where(MessageRow.contact_id == contact_id)
                .order_by(MessageRow.created_at.desc(), MessageRow.id.desc())
                .limit(limit)
            )
            return [Message.model_validate(row) for row in reversed(rows.all())]

    async def outgoing_times(
        self, since: datetime, limit: int, contact_id: int | None = None
    ) -> list[datetime]:
        if since.utcoffset() is None:
            msg = "the start of a window must be timezone-aware"
            raise ValueError(msg)
        statement = select(MessageRow.created_at).where(
            MessageRow.direction == MessageDirection.OUTGOING, MessageRow.created_at >= since
        )
        if contact_id is not None:
            statement = statement.where(MessageRow.contact_id == contact_id)
        statement = statement.order_by(MessageRow.created_at.desc(), MessageRow.id.desc())
        async with self._session_factory() as session:
            return list((await session.scalars(statement.limit(limit))).all())

    async def busiest_outgoing_contact(self, after: datetime) -> OutgoingPeak | None:
        if after.utcoffset() is None:
            msg = "the start of a window must be timezone-aware"
            raise ValueError(msg)
        count = func.count()
        statement = (
            select(MessageRow.contact_id, count)
            .where(MessageRow.direction == MessageDirection.OUTGOING, MessageRow.created_at > after)
            .group_by(MessageRow.contact_id)
            .order_by(count.desc(), MessageRow.contact_id)
            .limit(1)
        )
        async with self._session_factory() as session:
            found = (await session.execute(statement)).first()
            return None if found is None else OutgoingPeak(contact_id=found[0], count=found[1])


class SqlModeRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get_by_id(self, mode_id: int) -> Mode | None:
        async with self._session_factory() as session:
            row = await session.get(ModeRow, mode_id)
            return None if row is None else Mode.model_validate(row)

    async def get_by_name(self, name: str) -> Mode | None:
        async with self._session_factory() as session:
            row = await session.scalar(select(ModeRow).where(ModeRow.name == name))
            return None if row is None else Mode.model_validate(row)

    async def list_enabled(self) -> list[Mode]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ModeRow).where(ModeRow.enabled).order_by(ModeRow.id)
            )
            return [Mode.model_validate(row) for row in rows]

    async def create(self, data: ModeCreate) -> Mode:
        row = ModeRow(
            name=data.name,
            prompt_fragment=data.prompt_fragment,
            few_shot_examples=data.few_shot_examples,
            enabled=data.enabled,
        )
        try:
            async with self._session_factory.begin() as session:
                session.add(row)
                await session.flush()
                return Mode.model_validate(row)
        except IntegrityError:
            raise ModeAlreadyExists(data.name) from None

    async def resolve_by_id(self, mode_id: int | None) -> Mode:
        mode = None if mode_id is None else await self.get_by_id(mode_id)
        return await self._enabled_or_default(mode)

    async def resolve_by_name(self, name: str) -> Mode:
        return await self._enabled_or_default(await self.get_by_name(name))

    async def _enabled_or_default(self, mode: Mode | None) -> Mode:
        if mode is not None and mode.enabled:
            return mode
        default = await self.get_by_name(DEFAULT_MODE_NAME)
        if default is None or not default.enabled:
            raise DefaultModeUnavailable
        return default


class SqlPersonaRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get(self) -> Persona | None:
        async with self._session_factory() as session:
            row = await session.get(PersonaRow, PERSONA_ROW_ID)
            return None if row is None else Persona.model_validate(row)

    async def replace(self, text: str) -> Persona:
        statement = (
            insert(PersonaRow)
            .values(id=PERSONA_ROW_ID, text=text)
            .on_conflict_do_update(
                index_elements=[PersonaRow.id], set_={"text": text, "updated_at": func.now()}
            )
            .returning(PersonaRow)
        )
        async with self._session_factory.begin() as session:
            row = (await session.scalars(statement)).one()
            return Persona.model_validate(row)


class SqlScheduleProfileRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get(self) -> ScheduleProfile | None:
        async with self._session_factory() as session:
            row = await session.get(ScheduleProfileRow, SCHEDULE_PROFILE_ROW_ID)
            return None if row is None else _schedule_profile(row.data)

    async def replace(self, profile: ScheduleProfile) -> ScheduleProfile:
        data = profile.stored_data()
        statement = (
            insert(ScheduleProfileRow)
            .values(id=SCHEDULE_PROFILE_ROW_ID, data=data)
            .on_conflict_do_update(
                index_elements=[ScheduleProfileRow.id],
                set_={"data": data, "updated_at": func.now()},
            )
            .returning(ScheduleProfileRow)
        )
        async with self._session_factory.begin() as session:
            row = (await session.scalars(statement)).one()
            return _schedule_profile(row.data)


def _schedule_profile(data: dict[str, object]) -> ScheduleProfile:
    try:
        return ScheduleProfile.model_validate(data)
    except ValidationError:
        raise StoredScheduleInvalid from None


class SqlStyleExampleRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def add_many(
        self, examples: Sequence[StyleExampleCreate], *, replace: bool = False
    ) -> ExamplesAddResult:
        async with self._session_factory.begin() as session:
            if replace:
                await session.execute(delete(StyleExampleRow))
            added = await self._insert_new(session, examples)
        return ExamplesAddResult(added=added, skipped=len(examples) - added)

    async def list_all(self) -> list[StyleExample]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(StyleExampleRow).order_by(
                    StyleExampleRow.created_at.desc(), StyleExampleRow.id.desc()
                )
            )
            return [StyleExample.model_validate(row) for row in rows]

    async def count(self) -> int:
        async with self._session_factory() as session:
            return await session.scalar(select(func.count()).select_from(StyleExampleRow)) or 0

    async def delete_all(self) -> int:
        async with self._session_factory.begin() as session:
            removed = await session.scalars(delete(StyleExampleRow).returning(StyleExampleRow.id))
            return len(removed.all())

    @staticmethod
    async def _insert_new(session: AsyncSession, examples: Sequence[StyleExampleCreate]) -> int:
        if not examples:
            return 0
        statement = (
            insert(StyleExampleRow)
            .values(
                [
                    {
                        "tags": example.tags,
                        "turns": [turn.model_dump(mode="json") for turn in example.turns],
                        "content_hash": example.content_hash,
                    }
                    for example in examples
                ]
            )
            .on_conflict_do_nothing(index_elements=[StyleExampleRow.content_hash])
            .returning(StyleExampleRow.id)
        )
        return len((await session.scalars(statement)).all())


def _require_aware(moment: datetime) -> None:
    if moment.utcoffset() is None:
        msg = "an outbox time must be timezone-aware"
        raise ValueError(msg)


async def _locked_outbox_row(session: AsyncSession, row_id: int) -> ScheduledMessageRow:
    row = await session.get(ScheduledMessageRow, row_id, with_for_update=True)
    if row is None:
        raise OutboxRowNotFound(row_id)
    return row


def _require_state(row: ScheduledMessageRow, *allowed: OutboxState) -> None:
    if row.state not in allowed:
        raise OutboxStateConflict(row.id, row.state)


class SqlOutboxRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def enqueue(
        self,
        contact_id: int,
        text: str,
        send_at: datetime,
        source: OutboxSource,
        trigger_message_id: int | None = None,
    ) -> ScheduledMessage:
        _require_aware(send_at)
        insert_new = (
            insert(ScheduledMessageRow)
            .values(
                contact_id=contact_id,
                trigger_message_id=trigger_message_id,
                text=text,
                source=source,
                send_at=send_at,
                due_at=send_at,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    ScheduledMessageRow.contact_id,
                    ScheduledMessageRow.trigger_message_id,
                ],
                index_where=ScheduledMessageRow.trigger_message_id.is_not(None),
            )
            .returning(ScheduledMessageRow)
        )
        select_existing = select(ScheduledMessageRow).where(
            ScheduledMessageRow.contact_id == contact_id,
            ScheduledMessageRow.trigger_message_id == trigger_message_id,
        )
        try:
            async with self._session_factory.begin() as session:
                if await session.get(ContactRow, contact_id) is None:
                    raise ContactNotFound(contact_id)
                row = await session.scalar(insert_new)
                if row is None:
                    row = (await session.scalars(select_existing)).one()
                return ScheduledMessage.model_validate(row)
        except IntegrityError:
            raise OutboxRowInvalid from None

    async def get(self, row_id: int) -> ScheduledMessage | None:
        async with self._session_factory() as session:
            row = await session.get(ScheduledMessageRow, row_id)
            return None if row is None else ScheduledMessage.model_validate(row)

    async def claim_due(self, now: datetime) -> ScheduledMessage | None:
        _require_aware(now)
        due = (
            select(ScheduledMessageRow)
            .where(
                ScheduledMessageRow.state == OutboxState.SCHEDULED,
                ScheduledMessageRow.send_at <= now,
            )
            .order_by(ScheduledMessageRow.send_at, ScheduledMessageRow.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        async with self._session_factory.begin() as session:
            row = await session.scalar(due)
            if row is None:
                return None
            row.state = OutboxState.SENDING
            row.claimed_at = now
            await session.flush()
            return ScheduledMessage.model_validate(row)

    async def reschedule(
        self, row_id: int, send_at: datetime, error_class: str | None = None
    ) -> ScheduledMessage:
        _require_aware(send_at)
        async with self._session_factory.begin() as session:
            row = await _locked_outbox_row(session, row_id)
            _require_state(row, OutboxState.SENDING)
            row.state = OutboxState.SCHEDULED
            row.send_at = send_at
            row.claimed_at = None
            row.error_class = error_class
            await session.flush()
            return ScheduledMessage.model_validate(row)

    async def complete_send(
        self, row_id: int, contact_id: int, text: str, tg_message_id: int, sent_at: datetime
    ) -> ScheduledMessage:
        _require_aware(sent_at)
        store_message = (
            insert(MessageRow)
            .values(
                contact_id=contact_id,
                direction=MessageDirection.OUTGOING,
                text=text,
                tg_message_id=tg_message_id,
                created_at=sent_at,
            )
            .on_conflict_do_nothing(
                index_elements=[MessageRow.contact_id, MessageRow.tg_message_id]
            )
        )
        try:
            async with self._session_factory.begin() as session:
                row = await _locked_outbox_row(session, row_id)
                await session.execute(store_message)
                row.state = OutboxState.SENT
                row.sent_tg_message_id = tg_message_id
                row.error_class = None
                await session.flush()
                return ScheduledMessage.model_validate(row)
        except IntegrityError:
            raise ContactNotFound(contact_id) from None

    async def mark_cancelled(self, row_id: int, reason: str) -> ScheduledMessage:
        async with self._session_factory.begin() as session:
            row = await _locked_outbox_row(session, row_id)
            _require_state(row, OutboxState.SCHEDULED, OutboxState.SENDING)
            row.state = OutboxState.CANCELLED
            row.cancel_reason = reason
            await session.flush()
            return ScheduledMessage.model_validate(row)

    async def cancel_scheduled(self, row_id: int, reason: str) -> ScheduledMessage:
        async with self._session_factory.begin() as session:
            row = await _locked_outbox_row(session, row_id)
            _require_state(row, OutboxState.SCHEDULED)
            row.state = OutboxState.CANCELLED
            row.cancel_reason = reason
            await session.flush()
            return ScheduledMessage.model_validate(row)

    async def mark_failed(self, row_id: int, error_class: str) -> ScheduledMessage:
        async with self._session_factory.begin() as session:
            row = await _locked_outbox_row(session, row_id)
            _require_state(row, OutboxState.SENDING)
            row.state = OutboxState.FAILED
            row.error_class = error_class
            await session.flush()
            return ScheduledMessage.model_validate(row)

    async def retry_failed(self, row_id: int, send_at: datetime) -> ScheduledMessage:
        _require_aware(send_at)
        async with self._session_factory.begin() as session:
            row = await _locked_outbox_row(session, row_id)
            _require_state(row, OutboxState.FAILED)
            row.state = OutboxState.SCHEDULED
            row.send_at = send_at
            row.due_at = send_at
            row.claimed_at = None
            row.error_class = None
            row.cancel_reason = None
            await session.flush()
            return ScheduledMessage.model_validate(row)

    async def cancel_pending_for_contact(self, contact_id: int, reason: str) -> int:
        statement = (
            update(ScheduledMessageRow)
            .where(
                ScheduledMessageRow.contact_id == contact_id,
                ScheduledMessageRow.state == OutboxState.SCHEDULED,
            )
            .values(state=OutboxState.CANCELLED, cancel_reason=reason)
            .returning(ScheduledMessageRow.id)
        )
        async with self._session_factory.begin() as session:
            return len((await session.scalars(statement)).all())

    async def cancel_superseded_autopilot_rows(
        self, contact_id: int, trigger_message_id: int, reason: str
    ) -> int:
        async with self._session_factory.begin() as session:
            trigger = await session.get(MessageRow, trigger_message_id)
            if trigger is None or trigger.contact_id != contact_id:
                return 0
            older_triggers = select(MessageRow.id).where(
                MessageRow.contact_id == contact_id,
                tuple_(MessageRow.created_at, MessageRow.id)
                < tuple_(trigger.created_at, trigger.id),
            )
            statement = (
                update(ScheduledMessageRow)
                .where(
                    ScheduledMessageRow.contact_id == contact_id,
                    ScheduledMessageRow.source == OutboxSource.AUTOPILOT,
                    ScheduledMessageRow.state == OutboxState.SCHEDULED,
                    ScheduledMessageRow.trigger_message_id.in_(older_triggers),
                )
                .values(state=OutboxState.CANCELLED, cancel_reason=reason)
                .returning(ScheduledMessageRow.id)
            )
            return len((await session.scalars(statement)).all())

    async def has_sending_row(self, contact_id: int) -> bool:
        statement = select(
            select(ScheduledMessageRow.id)
            .where(
                ScheduledMessageRow.contact_id == contact_id,
                ScheduledMessageRow.state == OutboxState.SENDING,
            )
            .exists()
        )
        async with self._session_factory() as session:
            return bool(await session.scalar(statement))

    async def has_row_for_trigger(self, contact_id: int, trigger_message_id: int) -> bool:
        statement = select(
            select(ScheduledMessageRow.id)
            .where(
                ScheduledMessageRow.contact_id == contact_id,
                ScheduledMessageRow.trigger_message_id == trigger_message_id,
            )
            .exists()
        )
        async with self._session_factory() as session:
            return bool(await session.scalar(statement))

    async def stale_sending(self, claimed_before: datetime) -> list[ScheduledMessage]:
        _require_aware(claimed_before)
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ScheduledMessageRow)
                .where(
                    ScheduledMessageRow.state == OutboxState.SENDING,
                    ScheduledMessageRow.claimed_at <= claimed_before,
                )
                .order_by(ScheduledMessageRow.claimed_at, ScheduledMessageRow.id)
            )
            return [ScheduledMessage.model_validate(row) for row in rows]

    async def list_rows(self, state: OutboxState | None, limit: int) -> list[ScheduledMessage]:
        statement = select(ScheduledMessageRow).order_by(ScheduledMessageRow.id.desc()).limit(limit)
        if state is not None:
            statement = statement.where(ScheduledMessageRow.state == state)
        async with self._session_factory() as session:
            rows = await session.scalars(statement)
            return [ScheduledMessage.model_validate(row) for row in rows]

    async def count_by_state(self, source: OutboxSource | None = None) -> dict[OutboxState, int]:
        statement = select(ScheduledMessageRow.state, func.count()).group_by(
            ScheduledMessageRow.state
        )
        if source is not None:
            statement = statement.where(ScheduledMessageRow.source == source)
        async with self._session_factory() as session:
            found = {state: count for state, count in (await session.execute(statement)).all()}
        return {state: found.get(state, 0) for state in OutboxState}


async def _locked_sender_state(session: AsyncSession) -> SenderStateRow:
    row = await session.get(SenderStateRow, SENDER_STATE_ROW_ID, with_for_update=True)
    if row is None:
        raise SenderStateMissing
    return row


class SqlSenderStateRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get(self) -> SenderState:
        async with self._session_factory() as session:
            row = await session.get(SenderStateRow, SENDER_STATE_ROW_ID)
            if row is None:
                raise SenderStateMissing
            return SenderState.model_validate(row)

    async def pause_until(self, until: datetime, reason: str, *, now: datetime) -> bool:
        _require_aware(until)
        _require_aware(now)
        async with self._session_factory.begin() as session:
            row = await _locked_sender_state(session)
            is_new = row.paused_until is None or row.paused_until <= now
            if row.paused_until is None or until > row.paused_until:
                row.paused_until = until
                row.pause_reason = reason
                await session.flush()
            return is_new

    async def stop(self, reason: str) -> bool:
        async with self._session_factory.begin() as session:
            row = await _locked_sender_state(session)
            if row.stopped:
                return False
            row.stopped = True
            row.stop_reason = reason
            await session.flush()
            return True

    async def resume(self, *, keep_stop: bool = False) -> SenderState:
        async with self._session_factory.begin() as session:
            row = await _locked_sender_state(session)
            before = SenderState.model_validate(row)
            if before.stopped and keep_stop:
                return before
            row.paused_until = None
            row.pause_reason = None
            row.stopped = False
            row.stop_reason = None
            await session.flush()
            return before


async def _locked_autopilot_state(session: AsyncSession) -> AutopilotStateRow:
    row = await session.get(AutopilotStateRow, AUTOPILOT_STATE_ROW_ID, with_for_update=True)
    if row is None:
        raise AutopilotStateMissing
    return row


class SqlAutopilotStateRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get(self) -> AutopilotState:
        async with self._session_factory() as session:
            row = await session.get(AutopilotStateRow, AUTOPILOT_STATE_ROW_ID)
            if row is None:
                raise AutopilotStateMissing
            return AutopilotState.model_validate(row)

    async def stop(self, reason: AutopilotStopReason) -> bool:
        async with self._session_factory.begin() as session:
            row = await _locked_autopilot_state(session)
            if row.stopped:
                return False
            row.stopped = True
            row.stop_reason = reason.value
            await session.flush()
            return True

    async def pause_until(
        self, until: datetime, reason: AutopilotPauseReason, *, now: datetime
    ) -> bool:
        _require_aware(until)
        _require_aware(now)
        async with self._session_factory.begin() as session:
            row = await _locked_autopilot_state(session)
            is_new = row.paused_until is None or row.paused_until <= now
            if row.paused_until is None or until > row.paused_until:
                row.paused_until = until
                row.pause_reason = reason.value
                await session.flush()
            return is_new

    async def resume(self) -> AutopilotState:
        async with self._session_factory.begin() as session:
            row = await _locked_autopilot_state(session)
            before = AutopilotState.model_validate(row)
            row.stopped = False
            row.stop_reason = None
            row.paused_until = None
            row.pause_reason = None
            await session.flush()
            return before


def _latest_messages_of_active_contacts() -> type[MessageRow]:
    latest = (
        select(MessageRow)
        .join(ContactRow, ContactRow.id == MessageRow.contact_id)
        .where(ContactRow.status == ContactStatus.ACTIVE)
        .ext(distinct_on(MessageRow.contact_id))
        .order_by(MessageRow.contact_id, MessageRow.created_at.desc(), MessageRow.id.desc())
        .subquery()
    )
    return aliased(MessageRow, latest)


class SqlReplyBacklogRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def awaiting_reply(self, *, newer_than: datetime, older_than: datetime) -> list[Message]:
        _require_aware(newer_than)
        _require_aware(older_than)
        newest = _latest_messages_of_active_contacts()
        has_row = exists().where(
            ScheduledMessageRow.contact_id == newest.contact_id,
            ScheduledMessageRow.trigger_message_id == newest.id,
        )
        statement = (
            select(newest)
            .join(ContactRow, ContactRow.id == newest.contact_id)
            .where(
                newest.direction == MessageDirection.INCOMING,
                newest.created_at > newer_than,
                newest.created_at <= older_than,
                or_(ContactRow.reply_after.is_(None), newest.created_at > ContactRow.reply_after),
                ~has_row,
            )
            .order_by(newest.created_at, newest.id)
        )
        async with self._session_factory() as session:
            return [Message.model_validate(row) for row in await session.scalars(statement)]

    async def count_unanswered_active(self) -> int:
        newest = _latest_messages_of_active_contacts()
        statement = (
            select(func.count())
            .select_from(newest)
            .join(ContactRow, ContactRow.id == newest.contact_id)
            .where(
                newest.direction == MessageDirection.INCOMING,
                or_(ContactRow.reply_after.is_(None), newest.created_at > ContactRow.reply_after),
            )
        )
        async with self._session_factory() as session:
            return await session.scalar(statement) or 0
