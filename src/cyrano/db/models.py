from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from cyrano.domain.enums import (
    ContactGender,
    ContactStatus,
    MessageDirection,
    OutboxSource,
    OutboxState,
    PauseReason,
)

OUTBOX_VOCABULARY_LENGTH = 32
PAUSE_REASON_LENGTH = 32
GENDER_LENGTH = 16

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

EMPTY_JSON_OBJECT = text("'{}'::jsonb")
EMPTY_JSON_ARRAY = text("'[]'::jsonb")


def _enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


def _allowed_values(column: str, enum_type: type[StrEnum]) -> str:
    quoted = ", ".join(f"'{value}'" for value in _enum_values(enum_type))
    return f"{column} IN ({quoted})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012


class ModeRow(Base):
    __tablename__ = "modes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    prompt_fragment: Mapped[str] = mapped_column(Text)
    few_shot_examples: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, server_default=EMPTY_JSON_ARRAY
    )
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ContactRow(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        CheckConstraint(_allowed_values("pause_reason", PauseReason), name="pause_reason_allowed"),
        CheckConstraint(_allowed_values("gender", ContactGender), name="gender_allowed"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ContactStatus] = mapped_column(
        Enum(ContactStatus, name="contact_status", values_callable=_enum_values),
        server_default=ContactStatus.NEW.value,
        index=True,
    )
    pause_reason: Mapped[PauseReason | None] = mapped_column(
        Enum(
            PauseReason,
            native_enum=False,
            length=PAUSE_REASON_LENGTH,
            values_callable=_enum_values,
        )
    )
    mode_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey(ModeRow.id))
    gender: Mapped[ContactGender | None] = mapped_column(
        Enum(
            ContactGender,
            native_enum=False,
            length=GENDER_LENGTH,
            values_callable=_enum_values,
        )
    )
    profile: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=EMPTY_JSON_OBJECT)
    summary: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=EMPTY_JSON_OBJECT)
    summary_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reply_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MessageRow(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("contact_id", "tg_message_id"),
        Index(None, "contact_id", "created_at"),
        Index(None, "direction", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey(ContactRow.id))
    direction: Mapped[MessageDirection] = mapped_column(
        Enum(MessageDirection, name="message_direction", values_callable=_enum_values)
    )
    text: Mapped[str] = mapped_column(Text)
    tg_message_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


PERSONA_ROW_ID = 1
SENDER_STATE_ROW_ID = 1
SCHEDULE_PROFILE_ROW_ID = 1
AUTOPILOT_STATE_ROW_ID = 1


class PersonaRow(Base):
    __tablename__ = "persona"
    __table_args__ = (CheckConstraint(f"id = {PERSONA_ROW_ID}", name="single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    text: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StyleExampleRow(Base):
    __tablename__ = "style_examples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tags: Mapped[list[str]] = mapped_column(JSONB, server_default=EMPTY_JSON_ARRAY)
    turns: Mapped[list[dict[str, str]]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScheduledMessageRow(Base):
    __tablename__ = "scheduled_messages"
    __table_args__ = (
        CheckConstraint("text <> ''", name="text_not_empty"),
        CheckConstraint(_allowed_values("state", OutboxState), name="state_allowed"),
        CheckConstraint(_allowed_values("source", OutboxSource), name="source_allowed"),
        Index(
            "uq_scheduled_messages_contact_id_trigger_message_id",
            "contact_id",
            "trigger_message_id",
            unique=True,
            postgresql_where=text("trigger_message_id IS NOT NULL"),
        ),
        Index(None, "state", "send_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey(ContactRow.id))
    trigger_message_id: Mapped[int | None] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[OutboxSource] = mapped_column(
        Enum(
            OutboxSource,
            native_enum=False,
            length=OUTBOX_VOCABULARY_LENGTH,
            values_callable=_enum_values,
        )
    )
    send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[OutboxState] = mapped_column(
        Enum(
            OutboxState,
            native_enum=False,
            length=OUTBOX_VOCABULARY_LENGTH,
            values_callable=_enum_values,
        ),
        server_default=OutboxState.SCHEDULED.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error_class: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    follows_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("scheduled_messages.id"), index=True
    )


class SenderStateRow(Base):
    __tablename__ = "sender_state"
    __table_args__ = (CheckConstraint(f"id = {SENDER_STATE_ROW_ID}", name="single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_reason: Mapped[str | None] = mapped_column(Text)
    stopped: Mapped[bool] = mapped_column(Boolean, server_default=false())
    stop_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ScheduleProfileRow(Base):
    __tablename__ = "schedule_profile"
    __table_args__ = (CheckConstraint(f"id = {SCHEDULE_PROFILE_ROW_ID}", name="single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    data: Mapped[dict[str, object]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AutopilotStateRow(Base):
    __tablename__ = "autopilot_state"
    __table_args__ = (CheckConstraint(f"id = {AUTOPILOT_STATE_ROW_ID}", name="single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    stopped: Mapped[bool] = mapped_column(Boolean, server_default=false())
    stop_reason: Mapped[str | None] = mapped_column(Text)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
