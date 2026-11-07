from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from telethon.errors import UsernameInvalidError, UsernameNotOccupiedError
from telethon.tl.types import MessageEntityMentionName, MessageEntityTextUrl, MessageEntityUrl

from cyrano.domain.models import MAX_CARD_PHOTOS
from cyrano.services.profile_source import CardPhotos, LeomatchLookup, ProfileSourceError
from cyrano.userbot.leomatch_match import (
    USER_LINK,
    ChatMessage,
    UserLinkText,
    linked_name,
    looks_like_match_message,
    match_evidence,
    normalize_username,
)
from cyrano.userbot.profile_source import TELETHON_FAILURES
from cyrano.userbot.protocols import HistoryMessage, SenderLike, UserbotClient

logger = logging.getLogger("cyrano.userbot.leomatch_source")

SCAN_LIMIT = 100
SEARCH_RESULT_LIMIT = 5
CARD_WALK_BACK_POSITIONS = 2
CARD_HISTORY_LIMIT = 15
UTF16 = "utf-16-le"
UTF16_UNIT_BYTES = 2
UNRESOLVABLE_CHAT = (ValueError, UsernameNotOccupiedError, UsernameInvalidError)


def entity_text(text: str, offset: int, length: int) -> str:
    units = text.encode(UTF16, errors="surrogatepass")
    start = offset * UTF16_UNIT_BYTES
    end = (offset + length) * UTF16_UNIT_BYTES
    return units[start:end].decode(UTF16, errors="ignore")


def to_chat_message(message: HistoryMessage) -> ChatMessage:
    text = message.raw_text or ""
    user_ids: list[int] = []
    links: list[str] = []
    user_link_texts: list[UserLinkText] = []
    for entity in message.entities or ():
        if isinstance(entity, MessageEntityMentionName):
            user_ids.append(int(entity.user_id))
            covered = entity_text(text, int(entity.offset), int(entity.length))
            user_link_texts.append(UserLinkText(int(entity.user_id), covered))
        elif isinstance(entity, MessageEntityTextUrl):
            links.append(str(entity.url))
            user_link = USER_LINK.fullmatch(str(entity.url))
            if user_link is not None:
                covered = entity_text(text, int(entity.offset), int(entity.length))
                user_link_texts.append(UserLinkText(int(user_link.group(1)), covered))
        elif isinstance(entity, MessageEntityUrl):
            links.append(entity_text(text, int(entity.offset), int(entity.length)))
    return ChatMessage(
        id=message.id,
        out=bool(message.out),
        text=text,
        mentioned_user_ids=tuple(user_ids),
        links=tuple(links),
        user_link_texts=tuple(user_link_texts),
    )


def group_positions(newest_first: Iterable[HistoryMessage]) -> list[list[HistoryMessage]]:
    positions: list[list[HistoryMessage]] = []
    for message in newest_first:
        grouped_id = message.grouped_id
        if positions and grouped_id is not None and positions[-1][-1].grouped_id == grouped_id:
            positions[-1].append(message)
        else:
            positions.append([message])
    return [sorted(position, key=lambda member: member.id) for position in positions]


@dataclass(frozen=True, slots=True)
class _Card:
    text: str
    photo_messages: tuple[HistoryMessage, ...]


class TelethonCardPhotos:
    def __init__(self, client: UserbotClient, messages: Sequence[HistoryMessage]) -> None:
        self._client = client
        self._messages = tuple(messages)

    @property
    def count(self) -> int:
        return len(self._messages)

    async def download(self, index: int, destination: Path) -> bool:
        try:
            saved = await self._client.download_media(self._messages[index], file=str(destination))
        except TELETHON_FAILURES as exc:
            raise ProfileSourceError(type(exc).__name__) from None
        return bool(saved)


class TelethonLeomatchSource:
    def __init__(self, client: UserbotClient, bot_username: str) -> None:
        self._client = client
        self._bot_username = bot_username
        self._chat: SenderLike | None = None

    async def find_texts(self, tg_id: int, username: str | None) -> LeomatchLookup:
        try:
            chat = await self._resolve_chat()
            if chat is None:
                return LeomatchLookup()
            match = await self._find_match(chat, tg_id, normalize_username(username))
            if match is None:
                return LeomatchLookup()
            match_name = linked_name(match, tg_id)
            card = await self._find_card(chat, match)
            if card is None:
                return LeomatchLookup(match_text=match.text, match_name=match_name)
            return LeomatchLookup(
                card_text=card.text,
                match_text=match.text,
                match_name=match_name,
                card_photos=self._card_photos_of(card),
            )
        except TELETHON_FAILURES as exc:
            raise ProfileSourceError(type(exc).__name__) from None

    async def _resolve_chat(self) -> SenderLike | None:
        if self._chat is None:
            try:
                self._chat = await self._client.get_entity(self._bot_username)
            except UNRESOLVABLE_CHAT as exc:
                logger.debug("matching bot chat not resolved", extra={"error": type(exc).__name__})
                return None
        return self._chat

    async def _find_match(
        self, chat: SenderLike, tg_id: int, username: str | None
    ) -> ChatMessage | None:
        if username is not None:
            found = await self._search(chat, tg_id, username)
            if found is not None:
                return found
        return await self._scan(chat, tg_id, username)

    async def _search(self, chat: SenderLike, tg_id: int, username: str) -> ChatMessage | None:
        newest: ChatMessage | None = None
        for query in (username, f"@{username}"):
            async for raw in self._client.iter_messages(chat, SEARCH_RESULT_LIMIT, search=query):
                message = to_chat_message(raw)
                if match_evidence(message, tg_id, username) is None:
                    continue
                if newest is None or message.id > newest.id:
                    newest = message
        return newest

    async def _scan(self, chat: SenderLike, tg_id: int, username: str | None) -> ChatMessage | None:
        async for raw in self._client.iter_messages(chat, SCAN_LIMIT):
            message = to_chat_message(raw)
            if match_evidence(message, tg_id, username) is not None:
                return message
        return None

    def _card_photos_of(self, card: _Card) -> CardPhotos | None:
        if not card.photo_messages:
            return None
        return TelethonCardPhotos(self._client, card.photo_messages)

    async def _find_card(self, chat: SenderLike, match: ChatMessage) -> _Card | None:
        history = [
            raw
            async for raw in self._client.iter_messages(
                chat, CARD_HISTORY_LIMIT, offset_id=match.id
            )
        ]
        for position in group_positions(history)[:CARD_WALK_BACK_POSITIONS]:
            members = [to_chat_message(raw) for raw in position]
            if all(member.out for member in members):
                continue
            text = _first_caption(members)
            if text is None:
                continue
            if any(looks_like_match_message(member) for member in members):
                return None
            photos = [raw for raw in position if raw.photo is not None]
            return _Card(text, tuple(photos[:MAX_CARD_PHOTOS]))
        return None


def _first_caption(members: Sequence[ChatMessage]) -> str | None:
    for member in members:
        if member.text.strip():
            return member.text
    return None
