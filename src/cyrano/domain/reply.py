from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from cyrano.domain.enums import ContactGender, ReplyPurpose
from cyrano.domain.llm import LLMMessage, LLMUsage

MIN_HISTORY_MESSAGES = 4
REPLY_MAX_CHARS = 600
MAX_REPLY_PARTS = 3
REPLY_PART_JOINER = " "
FACT_NAME_LIMIT = 60
FACT_CITY_LIMIT = 60
FACT_ABOUT_LIMIT = 300
FACT_BIO_LIMIT = 300
REPLY_EMPTY_REASON = "empty after cleaning"
REPLY_BOUNDARY_REASON = "contains the dialogue boundary"
REPLY_HEADER_REASON = "contains a prompt layer header"
REPLY_SKIP_REASON = "contains the skip signal outside a proactive message"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ContactFacts(_Frozen):
    name: str | None = None
    age: int | None = None
    city: str | None = None
    about: str | None = None
    bio: str | None = None
    gender: ContactGender | None = None


class BuiltPrompt(_Frozen):
    messages: tuple[LLMMessage, ...]
    examples_offered: int = Field(ge=0)
    examples_kept: int = Field(ge=0)
    messages_offered: int = Field(ge=0)
    messages_kept: int = Field(ge=0)
    characters: int = Field(ge=0)
    purpose: ReplyPurpose = ReplyPurpose.REPLY
    wrap_up: bool = False


class GeneratedReply(_Frozen):
    text: str
    usage: LLMUsage
    needs_owner: bool = False
    skipped: bool = False
    truncated: bool = False
    examples_offered: int = Field(ge=0)
    examples_kept: int = Field(ge=0)
    messages_offered: int = Field(ge=0)
    messages_kept: int = Field(ge=0)
    prompt_characters: int = Field(ge=0)
