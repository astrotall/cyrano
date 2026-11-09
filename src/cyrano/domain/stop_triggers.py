from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from cyrano.domain.enums import MediaKind, MessageDirection, TriggerClass, TriggerSource

DETECTOR_MAX_TEXT_CHARS = 4096
HAND_OVER_HISTORY_LINES = 5
HAND_OVER_LINE_MAX_CHARS = 200
HAND_OVER_NAME_MAX_CHARS = 60
TRUNCATION_MARK = "…"
MEDIA_HAND_OVER_KINDS = frozenset(
    {
        MediaKind.VOICE,
        MediaKind.VIDEO_NOTE,
        MediaKind.PHOTO,
        MediaKind.VIDEO,
        MediaKind.OTHER,
    }
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StopDecision(_Frozen):
    classes: frozenset[TriggerClass] = frozenset()

    @property
    def triggered(self) -> bool:
        return bool(self.classes)

    def ordered(self) -> tuple[TriggerClass, ...]:
        return tuple(member for member in TriggerClass if member in self.classes)


class DialogueLine(_Frozen):
    direction: MessageDirection
    text: str = Field(repr=False, max_length=HAND_OVER_LINE_MAX_CHARS)


class HandOverNotice(_Frozen):
    contact_id: int
    name: str | None = Field(default=None, repr=False, max_length=HAND_OVER_NAME_MAX_CHARS)
    classes: tuple[TriggerClass, ...] = ()
    source: TriggerSource
    media_kind: MediaKind | None = None
    lines: tuple[DialogueLine, ...] = Field(default=(), max_length=HAND_OVER_HISTORY_LINES)


def truncate_line(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATION_MARK)] + TRUNCATION_MARK
