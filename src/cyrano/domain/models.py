"""Pydantic models the layers exchange."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field

from cyrano.domain.enums import ContactStatus, LeomatchTextKind, MessageDirection

DEFAULT_MODE_NAME = "default"

MAX_BIGINT = 2**63 - 1

MAX_USERNAME_LENGTH = 64
MAX_DISPLAY_NAME_LENGTH = 256
MAX_BIO_LENGTH = 1000
MAX_PHOTO_PATH_LENGTH = 256
MAX_LEOMATCH_TEXT_LENGTH = 4000
MAX_LEOMATCH_RELATED_TEXTS = 2
MAX_LEOMATCH_NAME_LENGTH = 100
MAX_LEOMATCH_CITY_LENGTH = 100
MIN_LEOMATCH_AGE = 18
MAX_LEOMATCH_AGE = 99


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


class ContactReadyEvent(_Input):
    contact_id: int


class TelegramProfile(_Input):
    username: str | None = None
    display_name: str | None = None
    bio: str | None = None


class LeomatchSourceText(_Input):
    kind: LeomatchTextKind
    text: str


class LeomatchProfile(_Input):
    raw_text: str = Field(max_length=MAX_LEOMATCH_TEXT_LENGTH)
    related_texts: list[Annotated[str, Field(max_length=MAX_LEOMATCH_TEXT_LENGTH)]] = Field(
        default_factory=list, max_length=MAX_LEOMATCH_RELATED_TEXTS
    )
    name: str | None = Field(default=None, max_length=MAX_LEOMATCH_NAME_LENGTH)
    age: int | None = Field(default=None, ge=MIN_LEOMATCH_AGE, le=MAX_LEOMATCH_AGE)
    city: str | None = Field(default=None, max_length=MAX_LEOMATCH_CITY_LENGTH)
    about: str | None = Field(default=None, max_length=MAX_LEOMATCH_TEXT_LENGTH)
    photo: str | None = Field(default=None, max_length=MAX_PHOTO_PATH_LENGTH)


class ContactProfile(_Input):
    username: str | None = Field(default=None, max_length=MAX_USERNAME_LENGTH)
    display_name: str | None = Field(default=None, max_length=MAX_DISPLAY_NAME_LENGTH)
    bio: str | None = Field(default=None, max_length=MAX_BIO_LENGTH)
    photo: str | None = Field(default=None, max_length=MAX_PHOTO_PATH_LENGTH)
    leomatch: LeomatchProfile | None = None
    collected_at: UtcDatetime | None = None
