from __future__ import annotations

from collections.abc import Iterable

from telethon import types

from cyrano.domain.enums import MediaKind
from cyrano.userbot.protocols import IncomingEvent

WEB_PREVIEW_ATTRIBUTE = "web_preview"
MEDIA_ATTRIBUTE = "media"
MESSAGE_ATTRIBUTE = "message"
STICKER_ATTRIBUTE = "sticker"
DOCUMENT_ATTRIBUTES_ATTRIBUTE = "attributes"
STICKER_EMOJI_ATTRIBUTE = "alt"

KIND_ATTRIBUTES: tuple[tuple[str, MediaKind], ...] = (
    ("voice", MediaKind.VOICE),
    ("video_note", MediaKind.VIDEO_NOTE),
    (STICKER_ATTRIBUTE, MediaKind.STICKER),
    ("photo", MediaKind.PHOTO),
    ("video", MediaKind.VIDEO),
    ("gif", MediaKind.VIDEO),
)

OTHER_MEDIA_ATTRIBUTES: tuple[str, ...] = (
    "document",
    "audio",
    "geo",
    "contact",
    "poll",
    "venue",
    "dice",
    "game",
    "invoice",
)

NOT_MEDIA_TYPES: tuple[type, ...] = (types.MessageMediaEmpty, types.MessageMediaWebPage)


def _read(source: object, name: str) -> object | None:
    try:
        return getattr(source, name, None)
    except Exception:
        return None


def _is_media(media: object | None) -> bool:
    return media is not None and not isinstance(media, NOT_MEDIA_TYPES)


def media_kind_of_message(message: object) -> MediaKind | None:
    if _read(message, WEB_PREVIEW_ATTRIBUTE) is not None:
        return None
    for name, kind in KIND_ATTRIBUTES:
        if _read(message, name) is not None:
            return kind
    if any(_read(message, name) is not None for name in OTHER_MEDIA_ATTRIBUTES):
        return MediaKind.OTHER
    if _is_media(_read(message, MEDIA_ATTRIBUTE)):
        return MediaKind.OTHER
    return None


def _attributes_of(sticker: object | None) -> Iterable[object]:
    attributes = _read(sticker, DOCUMENT_ATTRIBUTES_ATTRIBUTE)
    if isinstance(attributes, (list, tuple)):
        return attributes
    return ()


def sticker_emoji_of_message(message: object) -> str | None:
    for attribute in _attributes_of(_read(message, STICKER_ATTRIBUTE)):
        emoji = _read(attribute, STICKER_EMOJI_ATTRIBUTE)
        if isinstance(emoji, str) and emoji.strip():
            return emoji.strip()
    return None


def media_kind_of(event: IncomingEvent) -> MediaKind | None:
    message = _read(event, MESSAGE_ATTRIBUTE)
    if message is None:
        return None
    return media_kind_of_message(message)


def sticker_emoji_of(event: IncomingEvent) -> str | None:
    message = _read(event, MESSAGE_ATTRIBUTE)
    if message is None:
        return None
    return sticker_emoji_of_message(message)
