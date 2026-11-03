"""Vocabulary, limits and result types of the chat export converter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from cyrano.domain.enums import TurnRole

MAX_EXPORT_BYTES = 50 * 1024 * 1024
DEFAULT_GAP_HOURS = 6.0
MAX_GAP_HOURS = 24.0 * 366
DEFAULT_WINDOW = 6
MIN_WINDOW = 2
MAX_WINDOW = 50
DEFAULT_MAX_DIALOGUES = 100
MIN_NAME_LETTERS = 3
TURN_SEPARATOR = "\n"
OUTPUT_DIRECTORY = "data"
HTML_SUFFIXES = (".html", ".htm")
PRIVATE_FILE_MODE = 0o600
PART_SUFFIX = ".part"
TAG_SEPARATOR = ","


class SkipReason(StrEnum):
    UNREADABLE = "unreadable"
    NOT_A_MESSAGE = "not a message"
    FORWARDED = "forwarded"
    EMPTY_TEXT = "empty text"
    NO_SENDER = "no sender"
    NO_TIME = "no time"


class RedactionKind(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    LINK = "link"
    USERNAME = "username"
    NAME = "name"


def token_for(kind: RedactionKind) -> str:
    return f"<{kind.value}>"


@dataclass(frozen=True, slots=True)
class ExportMessage:
    time: datetime
    sender: str | None
    text: str
    sender_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChatTurn:
    role: TurnRole
    text: str


Dialogue = tuple[ChatTurn, ...]


@dataclass(frozen=True, slots=True)
class ConvertOptions:
    me: str | None = None
    me_id: str | None = None
    tags: tuple[str, ...] = ()
    max_dialogues: int = DEFAULT_MAX_DIALOGUES
    gap_hours: float = DEFAULT_GAP_HOURS
    window: int = DEFAULT_WINDOW
    redact_words: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConvertReport:
    messages_read: int = 0
    messages_kept: int = 0
    skipped: dict[SkipReason, int] = field(default_factory=dict)
    dropped_after_redaction: int = 0
    conversations: int = 0
    turns: int = 0
    truncations: int = 0
    candidates: int = 0
    duplicates: int = 0
    dialogues_written: int = 0
    redactions: dict[RedactionKind, int] = field(default_factory=dict)
