"""Pure cleaning of a model reply before anyone sees it."""

from __future__ import annotations

import re
import string

from cyrano.domain.errors import ReplyInvalid
from cyrano.domain.reply import (
    REPLY_BOUNDARY_REASON,
    REPLY_EMPTY_REASON,
    REPLY_HEADER_REASON,
    REPLY_MAX_CHARS,
)
from cyrano.prompts.texts import DIALOGUE_BOUNDARY, LAYER_HEADERS, NEEDS_OWNER_SIGNAL

QUOTE_PAIRS = (('"', '"'), ("“", "”"), ("«", "»"))
SPEAKER_NAMES = ("Me", "Я", "Assistant", "Ответ")
SPEAKER_PREFIX = re.compile(
    rf"^[*_#>\s]*(?:{'|'.join(SPEAKER_NAMES)})[*_\s]*:[*_\s]*", re.IGNORECASE
)
CODE_FENCE_LINE = re.compile(r"^[ \t]*```[^\n]*(?:\n|$)", re.MULTILINE)
HEADING_MARK = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)
BOLD_ASTERISKS = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
BOLD_UNDERSCORES = re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)")
ITALIC_ASTERISK = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])")
ITALIC_UNDERSCORE = re.compile(r"(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w_])")
BACKTICK = "`"
TRAILING_SPACES = re.compile(r"[ \t]+\n")
NEWLINE_RUN = re.compile(r"\n{3,}")
PARAGRAPH_BREAK = "\n\n"
SENTENCE_END = re.compile(r"[.!?…]+[\"'”»)]*(?=\s)")
LINE_BREAK = "\n"
WINDOWS_LINE_BREAK = "\r\n"
OLD_MAC_LINE_BREAK = "\r"
SIGNAL_WORD_SEPARATOR = "_"
NEEDS_OWNER_ANYWHERE = re.compile(
    r"[\s_\-]?".join(re.escape(part) for part in NEEDS_OWNER_SIGNAL.split(SIGNAL_WORD_SEPARATOR)),
    re.IGNORECASE,
)
SIGNAL_WRAPPERS = string.whitespace + string.punctuation + "«»“”„‘’…"


def _unwrap_quotes(text: str) -> tuple[str, bool]:
    for opening, closing in QUOTE_PAIRS:
        if len(text) >= 2 and text[0] == opening and text[-1] == closing:
            inner = text[1:-1]
            if opening not in inner and closing not in inner:
                return inner.strip(), True
    return text, False


def _strip_speaker_prefix(text: str) -> str:
    return SPEAKER_PREFIX.sub("", text, count=1).strip()


def _strip_wrappers(text: str) -> str:
    text, unwrapped = _unwrap_quotes(text)
    text = _strip_speaker_prefix(text)
    if not unwrapped:
        text, _ = _unwrap_quotes(text)
    return text


def _strip_markdown(text: str) -> str:
    text = CODE_FENCE_LINE.sub("", text)
    text = HEADING_MARK.sub("", text)
    for pattern in (BOLD_ASTERISKS, BOLD_UNDERSCORES, ITALIC_ASTERISK, ITALIC_UNDERSCORE):
        text = pattern.sub(r"\1", text)
    return text.replace(BACKTICK, "")


def _collapse_whitespace(text: str) -> str:
    text = text.replace(WINDOWS_LINE_BREAK, LINE_BREAK).replace(OLD_MAC_LINE_BREAK, LINE_BREAK)
    text = TRAILING_SPACES.sub(LINE_BREAK, text)
    return NEWLINE_RUN.sub(PARAGRAPH_BREAK, text)


def _cut_position(text: str, limit: int) -> int:
    window = text[: limit + 1]
    sentence_ends = [match.end() for match in SENTENCE_END.finditer(window) if match.end() <= limit]
    boundary = max([*sentence_ends, window.rfind(LINE_BREAK, 0, limit)])
    if boundary > 0:
        return boundary
    space = window.rfind(" ", 0, limit)
    return space if space > 0 else limit


def cap_length(text: str, limit: int = REPLY_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: _cut_position(text, limit)].strip()


def _leak_reason(text: str) -> str | None:
    if DIALOGUE_BOUNDARY in text:
        return REPLY_BOUNDARY_REASON
    if any(header in text for header in LAYER_HEADERS):
        return REPLY_HEADER_REASON
    return None


def is_bare_needs_owner(raw: str) -> bool:
    return raw.strip(SIGNAL_WRAPPERS).casefold() == NEEDS_OWNER_SIGNAL.casefold()


def contains_needs_owner(raw: str) -> bool:
    return NEEDS_OWNER_ANYWHERE.search(raw) is not None


def clean_reply(raw: str, limit: int = REPLY_MAX_CHARS) -> str:
    reason = _leak_reason(raw)
    if reason is not None:
        raise ReplyInvalid(reason)
    text = _strip_wrappers(_strip_markdown(raw).strip())
    text = cap_length(_collapse_whitespace(text).strip(), limit)
    if not text:
        raise ReplyInvalid(REPLY_EMPTY_REASON)
    reason = _leak_reason(text)
    if reason is not None:
        raise ReplyInvalid(reason)
    return text
