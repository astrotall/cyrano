"""SQLAlchemy models. Timestamps are timestamptz stored in UTC."""

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
    MetaData,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from cyrano.domain.enums import ContactStatus, MessageDirection

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

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ContactStatus] = mapped_column(
        Enum(ContactStatus, name="contact_status", values_callable=_enum_values),
        server_default=ContactStatus.NEW.value,
        index=True,
    )
    mode_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey(ModeRow.id))
    profile: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=EMPTY_JSON_OBJECT)
    summary: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=EMPTY_JSON_OBJECT)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MessageRow(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("contact_id", "tg_message_id"),
        Index(None, "contact_id", "created_at"),
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
