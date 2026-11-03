"""SQLAlchemy repositories. Every method is one transaction and returns domain models."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cyrano.db.models import (
    PERSONA_ROW_ID,
    ContactRow,
    MessageRow,
    ModeRow,
    PersonaRow,
    ScheduledMessageRow,
    StyleExampleRow,
)
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import ContactStatus, MessageDirection, OutboxSource, OutboxState
from cyrano.domain.errors import (
    ContactAlreadyExists,
    ContactNotFound,
    DefaultModeUnavailable,
    ModeAlreadyExists,
    ModeNotFound,
    OutboxRowInvalid,
    OutboxRowNotFound,
    OutboxStateConflict,
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
)
from cyrano.domain.outbox import ScheduledMessage
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
        self, contact_id: int, status: ContactStatus, *, mode_id: int | None = None
    ) -> Contact:
        async with self._session_factory.begin() as session:
            row = await _locked_contact(session, contact_id)
            mode_on_activation = mode_id if status is ContactStatus.ACTIVE else None
            has_mode = mode_on_activation is not None or row.mode_id is not None
            if transition_needed(row.status, status, has_mode=has_mode):
                if mode_on_activation is not None:
                    await _ensure_mode_exists(session, mode_on_activation)
                    row.mode_id = mode_on_activation
                row.status = status
                await session.flush()
            return Contact.model_validate(row)

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
        insert_new = (
            insert(MessageRow)
            .values(
                contact_id=data.contact_id,
                direction=data.direction,
                text=data.text,
                tg_message_id=data.tg_message_id,
            )
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
