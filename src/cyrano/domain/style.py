"""Persona and style example models, their limits and the content hash."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from cyrano.domain.enums import TurnRole
from cyrano.domain.models import UtcDatetime

NUL = "\x00"
MAX_PERSONA_LENGTH = 8000
MAX_TURN_LENGTH = 1000
MAX_TURNS_PER_DIALOGUE = 20
MAX_TAGS_PER_DIALOGUE = 5
MAX_TAG_LENGTH = 32
MAX_DIALOGUES_PER_FILE = 500
TAG_PATTERN = r"^[\w-]+$"
TAG_RULE = f"must be 1 to {MAX_TAG_LENGTH} letters, digits, underscores or hyphens (lowercased)"
NO_ME_TURN_RULE = f"needs at least one {TurnRole.ME.value!r} turn"
LAST_TURN_NOT_ME_RULE = f"the last turn must be {TurnRole.ME.value!r}"
HASH_ALGORITHM = "sha256"


def _remove_nul(value: object) -> object:
    return value.replace(NUL, "") if isinstance(value, str) else value


def _lowercase_without_nul(value: object) -> object:
    return value.replace(NUL, "").lower() if isinstance(value, str) else value


def _unique_sorted(tags: list[str]) -> list[str]:
    return sorted(set(tags))


Text = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TURN_LENGTH),
    BeforeValidator(_remove_nul),
]
PersonaText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_PERSONA_LENGTH),
    BeforeValidator(_remove_nul),
]
Tag = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=MAX_TAG_LENGTH, pattern=TAG_PATTERN
    ),
    BeforeValidator(_lowercase_without_nul),
]
Tags = Annotated[
    list[Tag],
    Field(max_length=MAX_TAGS_PER_DIALOGUE),
    AfterValidator(_unique_sorted),
]


class _Input(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class _Stored(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", from_attributes=True, hide_input_in_errors=True
    )


class Turn(_Input):
    role: TurnRole
    text: Text


class PersonaCreate(_Input):
    text: PersonaText


class Persona(_Stored):
    text: PersonaText
    updated_at: UtcDatetime


class StyleExampleCreate(_Input):
    tags: Tags = Field(default_factory=list)
    turns: list[Turn] = Field(min_length=1, max_length=MAX_TURNS_PER_DIALOGUE)

    @model_validator(mode="after")
    def _ends_with_my_reply(self) -> StyleExampleCreate:
        if all(turn.role is not TurnRole.ME for turn in self.turns):
            raise ValueError(NO_ME_TURN_RULE)
        if self.turns[-1].role is not TurnRole.ME:
            raise ValueError(LAST_TURN_NOT_ME_RULE)
        return self

    @property
    def content_hash(self) -> str:
        return content_hash(self.tags, self.turns)


class StyleExample(_Stored):
    id: int
    tags: Tags
    turns: list[Turn]
    created_at: UtcDatetime


class ExamplesAddResult(_Input):
    added: int = Field(ge=0)
    skipped: int = Field(ge=0)


def content_hash(tags: list[str], turns: list[Turn]) -> str:
    canonical = json.dumps(
        {
            "tags": sorted(set(tags)),
            "turns": [[turn.role.value, turn.text] for turn in turns],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.new(HASH_ALGORITHM, canonical.encode()).hexdigest()
