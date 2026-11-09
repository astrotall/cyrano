from __future__ import annotations

from datetime import UTC, datetime

import pytest
from telethon import types
from telethon.tl.custom import Message

from cyrano.domain.enums import MediaKind
from cyrano.userbot.handler import IncomingHandler, text_of, to_incoming_message
from cyrano.userbot.media_kind import (
    media_kind_of,
    media_kind_of_message,
    sticker_emoji_of,
    sticker_emoji_of_message,
)
from tests.userbot.fakes import (
    FAKE_MEDIA_OBJECT,
    FakeDocument,
    FakeEvent,
    FakeIntake,
    FakeMediaMessage,
    FakeSender,
    FakeStickerAttribute,
)

FAKE_EMOJI = "🫠"
FAKE_CAPTION = "invented-caption-marker-zq"
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
FIELD_NAMES = [
    "voice",
    "video_note",
    "sticker",
    "photo",
    "video",
    "gif",
    "document",
    "audio",
    "geo",
    "contact",
    "poll",
    "venue",
    "dice",
    "game",
    "invoice",
    "media",
]


def _document(*attributes: object) -> types.Document:
    return types.Document(
        id=1,
        access_hash=1,
        file_reference=b"",
        date=NOW,
        mime_type="application/octet-stream",
        size=1,
        dc_id=1,
        attributes=list(attributes),
    )


def _photo() -> types.Photo:
    return types.Photo(id=1, access_hash=1, file_reference=b"", date=NOW, sizes=[], dc_id=1)


def _message(media: types.TypeMessageMedia | None, text: str = "") -> Message:
    return Message(id=1, peer_id=types.PeerUser(1), message=text, media=media)


def _in_document(*attributes: object) -> Message:
    return _message(types.MessageMediaDocument(document=_document(*attributes)))


def _voice() -> Message:
    return _in_document(types.DocumentAttributeAudio(duration=1, voice=True))


def _video_note() -> Message:
    return _in_document(types.DocumentAttributeVideo(duration=1, w=1, h=1, round_message=True))


def _sticker(alt: str = FAKE_EMOJI) -> Message:
    return _in_document(
        types.DocumentAttributeSticker(alt=alt, stickerset=types.InputStickerSetEmpty())
    )


def _video_sticker() -> Message:
    return _in_document(
        types.DocumentAttributeSticker(alt=FAKE_EMOJI, stickerset=types.InputStickerSetEmpty()),
        types.DocumentAttributeVideo(duration=1, w=1, h=1),
    )


def _photo_message(text: str = "") -> Message:
    return _message(types.MessageMediaPhoto(photo=_photo()), text)


def _video() -> Message:
    return _in_document(types.DocumentAttributeVideo(duration=1, w=1, h=1))


def _gif() -> Message:
    return _in_document(
        types.DocumentAttributeAnimated(), types.DocumentAttributeVideo(duration=1, w=1, h=1)
    )


def _gif_without_video_attribute() -> Message:
    return _in_document(types.DocumentAttributeAnimated())


def _file() -> Message:
    return _in_document(types.DocumentAttributeFilename(file_name="invented.bin"))


def _audio_file() -> Message:
    return _in_document(types.DocumentAttributeAudio(duration=1))


def _location() -> Message:
    return _message(types.MessageMediaGeo(geo=types.GeoPoint(long=1.0, lat=1.0, access_hash=1)))


def _contact() -> Message:
    return _message(
        types.MessageMediaContact(
            phone_number="0", first_name="Fake", last_name="Fake", vcard="", user_id=0
        )
    )


def _dice() -> Message:
    return _message(types.MessageMediaDice(value=1, emoticon="x"))


def _unsupported() -> Message:
    return _message(types.MessageMediaUnsupported())


def _link_preview_with_photo() -> Message:
    return _message(
        types.MessageMediaWebPage(
            webpage=types.WebPage(
                id=1,
                url="https://example.invalid",
                display_url="example.invalid",
                hash=0,
                photo=_photo(),
            )
        ),
        "https://example.invalid",
    )


def _link_preview_with_document() -> Message:
    return _message(
        types.MessageMediaWebPage(
            webpage=types.WebPage(
                id=1,
                url="https://example.invalid",
                display_url="example.invalid",
                hash=0,
                document=_document(types.DocumentAttributeVideo(duration=1, w=1, h=1)),
            )
        ),
        "https://example.invalid",
    )


REAL_MESSAGES = [
    pytest.param(_voice, MediaKind.VOICE, id="voice"),
    pytest.param(_video_note, MediaKind.VIDEO_NOTE, id="video-note"),
    pytest.param(_sticker, MediaKind.STICKER, id="sticker"),
    pytest.param(_video_sticker, MediaKind.STICKER, id="video-sticker"),
    pytest.param(_photo_message, MediaKind.PHOTO, id="photo"),
    pytest.param(_video, MediaKind.VIDEO, id="video"),
    pytest.param(_gif, MediaKind.VIDEO, id="gif"),
    pytest.param(_gif_without_video_attribute, MediaKind.VIDEO, id="gif-without-video"),
    pytest.param(_file, MediaKind.OTHER, id="file"),
    pytest.param(_audio_file, MediaKind.OTHER, id="audio-file"),
    pytest.param(_location, MediaKind.OTHER, id="location"),
    pytest.param(_contact, MediaKind.OTHER, id="contact"),
    pytest.param(_dice, MediaKind.OTHER, id="dice"),
    pytest.param(_unsupported, MediaKind.OTHER, id="unsupported"),
    pytest.param(lambda: _message(None, "plain"), None, id="plain-text"),
    pytest.param(lambda: _message(types.MessageMediaEmpty()), None, id="empty-media"),
    pytest.param(_link_preview_with_photo, None, id="link-preview-with-photo"),
    pytest.param(_link_preview_with_document, None, id="link-preview-with-video"),
]


@pytest.mark.parametrize(("build", "expected"), REAL_MESSAGES)
def test_a_real_telethon_message_is_classified(build: object, expected: MediaKind | None) -> None:
    assert callable(build)

    assert media_kind_of_message(build()) is expected


def test_a_link_preview_really_exposes_a_photo_in_telethon() -> None:
    message = _link_preview_with_photo()

    assert message.photo is not None
    assert media_kind_of_message(message) is None


@pytest.mark.parametrize("field_name", FIELD_NAMES)
def test_every_other_attribute_alone_is_a_kind(field_name: str) -> None:
    message = FakeMediaMessage(**{field_name: FAKE_MEDIA_OBJECT})

    expected = {
        "voice": MediaKind.VOICE,
        "video_note": MediaKind.VIDEO_NOTE,
        "sticker": MediaKind.STICKER,
        "photo": MediaKind.PHOTO,
        "video": MediaKind.VIDEO,
        "gif": MediaKind.VIDEO,
    }.get(field_name, MediaKind.OTHER)
    assert media_kind_of_message(message) is expected


def test_a_message_with_nothing_is_not_media() -> None:
    assert media_kind_of_message(FakeMediaMessage()) is None


PRECEDENCE = [
    (["voice", "video_note", "sticker", "photo", "video", "document"], MediaKind.VOICE),
    (["video_note", "sticker", "photo", "video", "document"], MediaKind.VIDEO_NOTE),
    (["sticker", "photo", "video", "document"], MediaKind.STICKER),
    (["photo", "video", "gif", "document"], MediaKind.PHOTO),
    (["video", "gif", "document", "audio"], MediaKind.VIDEO),
    (["gif", "document"], MediaKind.VIDEO),
    (["document", "audio", "geo"], MediaKind.OTHER),
]


@pytest.mark.parametrize(("names", "expected"), PRECEDENCE)
def test_the_precedence_is_voice_video_note_sticker_photo_video_other(
    names: list[str], expected: MediaKind
) -> None:
    message = FakeMediaMessage(**dict.fromkeys(names, FAKE_MEDIA_OBJECT))

    assert media_kind_of_message(message) is expected


@pytest.mark.parametrize("field_name", FIELD_NAMES)
def test_a_web_page_preview_makes_the_message_plain_text(field_name: str) -> None:
    message = FakeMediaMessage(**{field_name: FAKE_MEDIA_OBJECT, "web_preview": FAKE_MEDIA_OBJECT})

    assert media_kind_of_message(message) is None


class _Exploding:
    def __getattr__(self, name: str) -> object:
        raise RuntimeError(name)


class _ExplodingVoice:
    @property
    def voice(self) -> object:
        raise ValueError

    photo = FAKE_MEDIA_OBJECT


@pytest.mark.parametrize(
    "odd",
    [
        pytest.param(None, id="none"),
        pytest.param(5, id="int"),
        pytest.param("text", id="str"),
        pytest.param(object(), id="object"),
        pytest.param(_Exploding(), id="exploding"),
        pytest.param([], id="list"),
        pytest.param({}, id="dict"),
        pytest.param(types.MessageMediaEmpty(), id="empty-media"),
    ],
)
def test_odd_objects_never_raise_and_are_not_media(odd: object) -> None:
    assert media_kind_of_message(odd) is None
    assert sticker_emoji_of_message(odd) is None


def test_an_attribute_that_raises_is_read_as_absent() -> None:
    assert media_kind_of_message(_ExplodingVoice()) is MediaKind.PHOTO


def test_the_sticker_emoji_is_read_from_the_sticker_attribute() -> None:
    assert sticker_emoji_of_message(_sticker()) == FAKE_EMOJI
    assert sticker_emoji_of_message(_sticker("  ")) is None
    assert sticker_emoji_of_message(_sticker("")) is None


def test_a_message_that_is_not_a_sticker_has_no_sticker_emoji() -> None:
    assert sticker_emoji_of_message(_voice()) is None
    assert sticker_emoji_of_message(_photo_message(FAKE_CAPTION)) is None


@pytest.mark.parametrize(
    "attributes",
    [
        [FakeStickerAttribute(alt=None)],
        [object()],
        "not a list",
        None,
        [FakeStickerAttribute(alt=None), FakeStickerAttribute(alt=FAKE_EMOJI)],
    ],
)
def test_the_sticker_emoji_survives_odd_attribute_lists(attributes: object) -> None:
    sticker = FakeDocument()
    sticker.attributes = attributes  # type: ignore[assignment]

    found = sticker_emoji_of_message(FakeMediaMessage(sticker=sticker))

    assert found == (FAKE_EMOJI if isinstance(attributes, list) and len(attributes) == 2 else None)


def test_the_event_without_a_message_is_not_media() -> None:
    event = FakeEvent(message=None)

    assert media_kind_of(event) is None
    assert sticker_emoji_of(event) is None


def test_the_event_reads_its_message() -> None:
    assert media_kind_of(FakeEvent(message=_voice())) is MediaKind.VOICE
    assert sticker_emoji_of(FakeEvent(message=_sticker())) == FAKE_EMOJI


def test_a_text_message_converts_without_a_kind() -> None:
    incoming = to_incoming_message(FakeEvent(message=_message(None, "plain")), FakeSender())

    assert incoming.media_kind is None
    assert incoming.text == FakeEvent().raw_text


@pytest.mark.parametrize(
    ("build", "kind"),
    [
        (_voice, MediaKind.VOICE),
        (_video_note, MediaKind.VIDEO_NOTE),
        (_photo_message, MediaKind.PHOTO),
        (_video, MediaKind.VIDEO),
        (_file, MediaKind.OTHER),
    ],
)
def test_a_media_message_without_a_caption_converts_with_an_empty_text(
    build: object, kind: MediaKind
) -> None:
    assert callable(build)
    event = FakeEvent(raw_text="", message=build())

    incoming = to_incoming_message(event, FakeSender())

    assert (incoming.media_kind, incoming.text) == (kind, "")


def test_a_caption_stays_the_text_of_a_photo() -> None:
    event = FakeEvent(raw_text=FAKE_CAPTION, message=_photo_message(FAKE_CAPTION))

    incoming = to_incoming_message(event, FakeSender())

    assert (incoming.media_kind, incoming.text) == (MediaKind.PHOTO, FAKE_CAPTION)


def test_a_sticker_with_an_emoji_is_stored_as_the_emoji() -> None:
    event = FakeEvent(raw_text="", message=_sticker())

    incoming = to_incoming_message(event, FakeSender())

    assert (incoming.media_kind, incoming.text) == (MediaKind.STICKER, FAKE_EMOJI)


def test_a_sticker_without_an_emoji_has_no_text() -> None:
    event = FakeEvent(raw_text=None, message=_sticker(""))

    incoming = to_incoming_message(event, FakeSender())

    assert (incoming.media_kind, incoming.text) == (MediaKind.STICKER, "")


def test_the_text_of_a_non_sticker_never_takes_an_emoji() -> None:
    event = FakeEvent(raw_text="", message=_voice())

    assert text_of(event, MediaKind.VOICE) == ""
    assert text_of(event, None) == ""


async def test_the_handler_passes_the_kind_to_the_intake_and_writes_nothing() -> None:
    intake = FakeIntake()
    event = FakeEvent(id=31, raw_text=FAKE_CAPTION, message=_photo_message(FAKE_CAPTION))

    await IncomingHandler(intake).handle(event)

    [received] = intake.received
    assert (received.media_kind, received.text, received.tg_message_id) == (
        MediaKind.PHOTO,
        FAKE_CAPTION,
        31,
    )
    assert event.write_attempts == []


async def test_a_message_that_cannot_be_read_is_still_stored_as_text() -> None:
    intake = FakeIntake()
    handler = IncomingHandler(intake)

    await handler.handle(FakeEvent(message=_Exploding()))

    [received] = intake.received
    assert received.media_kind is None
    assert (handler.handled, handler.failed) == (1, 0)
