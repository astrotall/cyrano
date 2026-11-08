from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.config import JsonDict

from cyrano.domain.enums import ContactStatus, SummaryOutcome, SummaryReason
from cyrano.domain.llm import LLMMessage
from cyrano.domain.models import MAX_BIGINT, UtcDatetime

SUMMARY_VERSION: Final = 1
SUMMARY_TEXT_MAX_CHARS = 200
SUMMARY_ITEM_MAX_CHARS = 120
SUMMARY_INTERESTS_MAX = 10
SUMMARY_AGREEMENTS_MAX = 8
SUMMARY_OPEN_QUESTIONS_MAX = 5
SUMMARY_TOLD_ABOUT_OWNER_MAX = 10
SUMMARY_NOTES_MAX = 8
SUMMARY_MIN_AGE = 10
SUMMARY_MAX_AGE = 99
SUMMARY_CHUNK_MESSAGES = 40
SUMMARY_FAILURE_COOLDOWN_SECONDS = 600
SUMMARY_PROMPT_MAX_CHARS = 24000
SUMMARY_PHONE_MIN_DIGITS = 7
SUMMARY_LINK_PREFIXES = ("http://", "https://", "www.")
SUMMARY_SWEEP_LIMIT = 20
SUMMARY_ELIGIBLE_STATUSES = frozenset({ContactStatus.ACTIVE, ContactStatus.PAUSED})
SUMMARY_JOB_NAME = "update_contact_summary"
SUMMARY_JOB_ID_TEMPLATE = "summary-{contact_id}-{up_to_message_id}"
NUL = "\x00"

SUMMARY_TEXT_FIELDS = ("name", "city", "occupation", "mood_and_tone")
SUMMARY_LIST_CAPS: Mapping[str, int] = MappingProxyType(
    {
        "interests": SUMMARY_INTERESTS_MAX,
        "agreements_and_plans": SUMMARY_AGREEMENTS_MAX,
        "open_questions": SUMMARY_OPEN_QUESTIONS_MAX,
        "told_about_owner": SUMMARY_TOLD_ABOUT_OWNER_MAX,
        "notes": SUMMARY_NOTES_MAX,
    }
)
AGE_FIELD = "age"


def summary_job_id(contact_id: int, up_to_message_id: int) -> str:
    return SUMMARY_JOB_ID_TEMPLATE.format(contact_id=contact_id, up_to_message_id=up_to_message_id)


def _clean(value: str, limit: int) -> str | None:
    collapsed = " ".join(value.replace(NUL, "").split())
    return collapsed[:limit].rstrip() or None


def _text(value: object) -> str | None:
    return _clean(value, SUMMARY_TEXT_MAX_CHARS) if isinstance(value, str) else None


def _items(value: object, cap: int) -> list[str]:
    raw = [value] if isinstance(value, str) else value
    if not isinstance(raw, list):
        return []
    kept: list[str] = []
    for item in raw:
        cleaned = _clean(item, SUMMARY_ITEM_MAX_CHARS) if isinstance(item, str) else None
        if cleaned is not None and cleaned not in kept:
            kept.append(cleaned)
    return kept[:cap]


def _age(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdecimal():
        number = int(value.strip())
    else:
        return None
    return number if SUMMARY_MIN_AGE <= number <= SUMMARY_MAX_AGE else None


def _text_schema() -> JsonDict:
    return {"maxLength": SUMMARY_TEXT_MAX_CHARS}


def _list_schema(cap: int) -> JsonDict:
    return {"maxItems": cap, "items": {"type": "string", "maxLength": SUMMARY_ITEM_MAX_CHARS}}


def _age_schema() -> JsonDict:
    return {"minimum": SUMMARY_MIN_AGE, "maximum": SUMMARY_MAX_AGE}


class ContactSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str | None = Field(default=None, json_schema_extra=_text_schema())
    age: int | None = Field(default=None, json_schema_extra=_age_schema())
    city: str | None = Field(default=None, json_schema_extra=_text_schema())
    occupation: str | None = Field(default=None, json_schema_extra=_text_schema())
    interests: list[str] = Field(
        default_factory=list,
        json_schema_extra=_list_schema(SUMMARY_INTERESTS_MAX),
    )
    mood_and_tone: str | None = Field(default=None, json_schema_extra=_text_schema())
    agreements_and_plans: list[str] = Field(
        default_factory=list,
        json_schema_extra=_list_schema(SUMMARY_AGREEMENTS_MAX),
    )
    open_questions: list[str] = Field(
        default_factory=list,
        json_schema_extra=_list_schema(SUMMARY_OPEN_QUESTIONS_MAX),
    )
    told_about_owner: list[str] = Field(
        default_factory=list,
        json_schema_extra=_list_schema(SUMMARY_TOLD_ABOUT_OWNER_MAX),
    )
    notes: list[str] = Field(
        default_factory=list,
        json_schema_extra=_list_schema(SUMMARY_NOTES_MAX),
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: object) -> object:
        if not isinstance(data, Mapping):
            return data
        normalized: dict[str, object] = {
            name: _text(data.get(name)) for name in SUMMARY_TEXT_FIELDS
        }
        normalized[AGE_FIELD] = _age(data.get(AGE_FIELD))
        for name, cap in SUMMARY_LIST_CAPS.items():
            normalized[name] = _items(data.get(name), cap)
        return normalized

    def is_empty(self) -> bool:
        texts = (getattr(self, name) for name in SUMMARY_TEXT_FIELDS)
        lists = (getattr(self, name) for name in SUMMARY_LIST_CAPS)
        return self.age is None and not any(texts) and not any(lists)


class SummaryCursor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    created_at: UtcDatetime
    message_id: int = Field(gt=0, le=MAX_BIGINT)


class StoredSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    version: Literal[1]
    updated_at: UtcDatetime
    cursor: SummaryCursor
    data: ContactSummary


def read_stored_summary(raw: Mapping[str, object] | None) -> StoredSummary | None:
    if not raw:
        return None
    try:
        return StoredSummary.model_validate(raw)
    except ValidationError:
        return None


class SummaryPrompt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: tuple[LLMMessage, ...]
    messages_offered: int = Field(ge=0)
    messages_kept: int = Field(ge=0)
    characters: int = Field(ge=0)


class SummaryDue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contact_id: int
    waiting: int = Field(ge=0)
    up_to_message_id: int


class SummaryJobRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contact_id: int = Field(gt=0, le=MAX_BIGINT)
    up_to_message_id: int = Field(gt=0, le=MAX_BIGINT)


class SummaryUpdateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: SummaryOutcome
    reason: SummaryReason
    messages: int = Field(default=0, ge=0)
    messages_in_prompt: int = Field(default=0, ge=0)
    remaining: int | None = Field(default=None, ge=0)
    removed: int = Field(default=0, ge=0)
    error: str | None = None
