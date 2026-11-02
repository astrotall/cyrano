"""Pydantic models the layers exchange."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field

from cyrano.domain.enums import ContactStatus, MessageDirection

DEFAULT_MODE_NAME = "default"

MAX_BIGINT = 2**63 - 1


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
TelegramId = Annotated[int, Field(gt=0, le=MAX_BIGINT)]


class _Stored(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)


class _Input(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Contact(_Stored):
    id: int
    tg_id: int
    username: str | None
    display_name: str | None
    status: ContactStatus
    mode_id: int | None
    profile: dict[str, object]
    summary: dict[str, object]
    last_message_at: UtcDatetime | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ContactCreate(_Input):
    tg_id: TelegramId
    username: str | None = None
    display_name: str | None = None
    profile: dict[str, object] = Field(default_factory=dict)


class ContactUpdate(_Input):
    profile: dict[str, object] | None = None
    summary: dict[str, object] | None = None


class Message(_Stored):
    id: int
    contact_id: int
    direction: MessageDirection
    text: str
    tg_message_id: int
    created_at: UtcDatetime


class MessageCreate(_Input):
    contact_id: int
    direction: MessageDirection
    text: str
    tg_message_id: TelegramId


class Mode(_Stored):
    id: int
    name: str
    prompt_fragment: str
    few_shot_examples: list[dict[str, object]]
    enabled: bool
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ModeCreate(_Input):
    name: str = Field(min_length=1)
    prompt_fragment: str
    few_shot_examples: list[dict[str, object]] = Field(default_factory=list)
    enabled: bool = True


class IncomingMessage(_Input):
    tg_id: TelegramId
    username: str | None = None
    display_name: str | None = None
    tg_message_id: TelegramId
    text: str
    sent_at: UtcDatetime


class NewContactEvent(_Input):
    contact_id: int
    message_id: int
