"""Read-only lookup of a person's texts in the chat with the matching bot."""

from __future__ import annotations

import logging
from pathlib import Path

from telethon.errors import UsernameInvalidError, UsernameNotOccupiedError
from telethon.tl.types import MessageEntityMentionName, MessageEntityTextUrl, MessageEntityUrl

from cyrano.domain.enums import LeomatchTextKind
from cyrano.domain.models import LeomatchSourceText
from cyrano.services.profile_source import CardPhoto, LeomatchLookup, ProfileSourceError
from cyrano.userbot.leomatch_match import (
    ChatMessage,
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
    for entity in message.entities or ():
        if isinstance(entity, MessageEntityMentionName):
            user_ids.append(int(entity.user_id))
        elif isinstance(entity, MessageEntityTextUrl):
            links.append(str(entity.url))
        elif isinstance(entity, MessageEntityUrl):
            links.append(entity_text(text, int(entity.offset), int(entity.length)))
    return ChatMessage(
        id=message.id,
        out=bool(message.out),
        text=text,
        mentioned_user_ids=tuple(user_ids),
        links=tuple(links),
    )


class TelethonCardPhoto:
    def __init__(self, client: UserbotClient, message: HistoryMessage) -> None:
        self._client = client
        self._message = message

    async def download(self, destination: Path) -> bool:
        try:
            saved = await self._client.download_media(self._message, file=str(destination))
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
            texts = [LeomatchSourceText(kind=LeomatchTextKind.MATCH, text=match.text)]
            card = await self._find_card(chat, match)
            if card is None:
                return LeomatchLookup(texts)
            texts.append(
                LeomatchSourceText(kind=LeomatchTextKind.CARD, text=to_chat_message(card).text)
            )
            return LeomatchLookup(texts, self._card_photo_of(card))
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

    def _card_photo_of(self, card: HistoryMessage) -> CardPhoto | None:
        return None if card.photo is None else TelethonCardPhoto(self._client, card)

    async def _find_card(self, chat: SenderLike, match: ChatMessage) -> HistoryMessage | None:
        async for raw in self._client.iter_messages(
            chat, CARD_WALK_BACK_POSITIONS, offset_id=match.id
        ):
            message = to_chat_message(raw)
            if message.out or not message.text.strip():
                continue
            return None if looks_like_match_message(message) else raw
        return None
