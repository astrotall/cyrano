"""Pure decisions about which messages of the matching-bot chat belong to one person."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

USER_LINK = re.compile(r"tg://user\?id=([0-9]{1,19})")
TME_LINK = re.compile(r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(\w++)(?!/\w)", re.IGNORECASE)
ANY_TME_LINK = re.compile(r"(?:t|telegram)\.me/", re.IGNORECASE)
USERNAME_PREFIX = "@"
LINE_BREAK = "\n"


class MatchEvidence(StrEnum):
    TELEGRAM_ID = "telegram_id"
    USERNAME = "username"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    id: int
    out: bool
    text: str
    mentioned_user_ids: tuple[int, ...] = ()
    links: tuple[str, ...] = ()

    @property
    def searchable(self) -> str:
        return LINE_BREAK.join((self.text, *self.links))


def normalize_username(username: str | None) -> str | None:
    if username is None:
        return None
    return username.strip().removeprefix(USERNAME_PREFIX).strip() or None


def _linked_user_ids(message: ChatMessage) -> set[int]:
    found = {int(raw) for raw in USER_LINK.findall(message.searchable)}
    return found | set(message.mentioned_user_ids)


def _mentions_username(message: ChatMessage, username: str) -> bool:
    pattern = re.compile(rf"(?<!\w){re.escape(USERNAME_PREFIX + username)}(?!\w)", re.IGNORECASE)
    return pattern.search(message.text) is not None


def _links_to_username(message: ChatMessage, username: str) -> bool:
    wanted = username.casefold()
    return any(m.group(1).casefold() == wanted for m in TME_LINK.finditer(message.searchable))


def match_evidence(message: ChatMessage, tg_id: int, username: str | None) -> MatchEvidence | None:
    if message.out:
        return None
    user_ids = _linked_user_ids(message)
    if tg_id in user_ids:
        return MatchEvidence.TELEGRAM_ID
    if user_ids:
        return None
    name = normalize_username(username)
    if name is None:
        return None
    if _mentions_username(message, name) or _links_to_username(message, name):
        return MatchEvidence.USERNAME
    return None


def looks_like_match_message(message: ChatMessage) -> bool:
    return bool(_linked_user_ids(message)) or ANY_TME_LINK.search(message.searchable) is not None
