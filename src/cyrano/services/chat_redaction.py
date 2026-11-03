"""Replacing e-mails, phones, links, usernames and names with neutral tokens; pure."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from cyrano.domain.chat_export import MIN_NAME_LETTERS, RedactionKind, token_for

LETTER_RUN = re.compile(r"[^\W\d_]+")
EMAIL_PATTERN = r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
LINK_TAIL = r"(?=[.,;:!?)\]»\"'…]*(?:\s|$))"
LINK_HOSTS = (
    "t.me",
    "telegram.me",
    "telegram.org",
    "wa.me",
    "instagram.com",
    "vk.com",
    "facebook.com",
    "fb.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "twitter.com",
    "x.com",
    "ok.ru",
)
_HOSTS = "|".join(re.escape(host) for host in LINK_HOSTS)
LINK_PATTERN = (
    rf"(?:(?:https?://|www\.)\S+?{LINK_TAIL}"
    rf"|(?<![\w.-])(?:{_HOSTS})(?:/\S*?{LINK_TAIL})?"
    rf"|(?<![\w.-])[\w-]+(?:\.[\w-]+)*\.[a-z]{{2,}}/\S*?{LINK_TAIL})"
)
PHONE_PATTERN = r"\+?\(?\d(?:[ \u00a0\-()]*\d){6,}"
USERNAME_PATTERN = r"(?<![\w@])@\w+"
NAME_GROUP = "(?<!\\w)(?:{words})(?!\\w)"
TOKEN_PATTERN = re.compile("|".join(re.escape(token_for(kind)) for kind in RedactionKind))


@dataclass(frozen=True, slots=True)
class RedactedText:
    text: str
    counts: dict[RedactionKind, int]

    @property
    def only_tokens(self) -> bool:
        if not self.counts:
            return False
        remainder = TOKEN_PATTERN.sub("", self.text)
        return not any(character.isalnum() for character in remainder)


def name_words(names: Iterable[str], minimum_letters: int) -> list[str]:
    words: dict[str, str] = {}
    for name in names:
        for word in LETTER_RUN.findall(name):
            if len(word) >= minimum_letters:
                words.setdefault(word.casefold(), word)
    return sorted(words.values(), key=lambda word: (-len(word), word.casefold()))


class Redactor:
    def __init__(self, sender_names: Iterable[str], extra_words: Iterable[str] = ()) -> None:
        words = name_words(sender_names, MIN_NAME_LETTERS)
        explicit = name_words(extra_words, 1)
        combined = {word.casefold(): word for word in (*words, *explicit)}
        ordered = sorted(combined.values(), key=lambda word: (-len(word), word.casefold()))
        groups = [
            (RedactionKind.EMAIL, EMAIL_PATTERN),
            (RedactionKind.LINK, LINK_PATTERN),
            (RedactionKind.PHONE, PHONE_PATTERN),
            (RedactionKind.USERNAME, USERNAME_PATTERN),
        ]
        if ordered:
            alternatives = "|".join(re.escape(word) for word in ordered)
            groups.append((RedactionKind.NAME, NAME_GROUP.format(words=alternatives)))
        self._pattern = re.compile(
            "|".join(f"(?P<{kind.value}>{pattern})" for kind, pattern in groups),
            re.IGNORECASE,
        )

    def redact(self, text: str) -> RedactedText:
        counts: Counter[RedactionKind] = Counter()

        def replace(match: re.Match[str]) -> str:
            kind = RedactionKind(match.lastgroup or "")
            counts[kind] += 1
            return token_for(kind)

        return RedactedText(self._pattern.sub(replace, text), dict(counts))
