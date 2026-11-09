from __future__ import annotations

import pytest

from cyrano.domain.enums import MediaKind, MessageDirection, ReplyPurpose
from cyrano.domain.models import Message
from cyrano.domain.reply import ContactFacts
from cyrano.prompts.render import media_text, render_summary_user_prompt
from cyrano.prompts.texts import (
    DIALOGUE_BEGIN,
    DIALOGUE_BOUNDARY,
    DIALOGUE_END,
    MEDIA_LABELS,
    SUMMARY_BOUNDARY,
    SUMMARY_MESSAGES_BEGIN,
    SUMMARY_MESSAGES_END,
)
from tests.reply_data import (
    INCOMING,
    OUTGOING,
    make_builder,
    make_history,
    make_message,
    user_of,
)

CAPTION = "invented-label-caption-marker-zq"
EXPECTED_LABELS = {
    MediaKind.VOICE: "[голосовое сообщение]",
    MediaKind.VIDEO_NOTE: "[кружок]",
    MediaKind.PHOTO: "[фото]",
    MediaKind.VIDEO: "[видео]",
    MediaKind.STICKER: "[стикер]",
    MediaKind.OTHER: "[вложение]",
}


def _media(
    index: int, kind: MediaKind | None, text: str = "", direction: MessageDirection = INCOMING
) -> Message:
    return make_message(index, direction, text).model_copy(update={"media_kind": kind})


def _reply_dialogue(messages: list[Message]) -> list[str]:
    prompt = make_builder().build(
        contact_id=7,
        persona="Invented persona.",
        mode_fragment="Invented mode.",
        facts=ContactFacts(),
        summary=None,
        messages=messages,
        examples=[],
    )
    lines = user_of(prompt).splitlines()
    return lines[lines.index(DIALOGUE_BEGIN) + 1 : lines.index(DIALOGUE_END)]


def _proactive_dialogue(messages: list[Message]) -> list[str]:
    prompt = make_builder().build(
        contact_id=7,
        persona="Invented persona.",
        mode_fragment="Invented mode.",
        facts=ContactFacts(),
        summary=None,
        messages=messages,
        examples=[],
        purpose=ReplyPurpose.PROACTIVE,
    )
    lines = user_of(prompt).splitlines()
    return lines[lines.index(DIALOGUE_BEGIN) + 1 : lines.index(DIALOGUE_END)]


def _summary_dialogue(messages: list[Message]) -> list[str]:
    user = render_summary_user_prompt(previous=None, messages=messages)
    block = user.split(SUMMARY_MESSAGES_BEGIN)[1].split(SUMMARY_MESSAGES_END)[0]
    return block.strip("\n").splitlines()


def test_the_labels_are_the_bracketed_russian_words_of_the_ticket() -> None:
    assert MEDIA_LABELS == EXPECTED_LABELS
    assert set(MEDIA_LABELS) == set(MediaKind)


@pytest.mark.parametrize("kind", list(MediaKind))
def test_media_text_without_a_caption_is_the_label_alone(kind: MediaKind) -> None:
    assert media_text("", kind) == EXPECTED_LABELS[kind]
    assert media_text("  \n ", kind) == EXPECTED_LABELS[kind]


@pytest.mark.parametrize("kind", list(MediaKind))
def test_media_text_with_a_caption_is_the_label_then_the_caption(kind: MediaKind) -> None:
    assert media_text(CAPTION, kind) == f"{EXPECTED_LABELS[kind]} {CAPTION}"
    assert media_text(f"  {CAPTION}\n", kind) == f"{EXPECTED_LABELS[kind]} {CAPTION}"


def test_media_text_without_a_kind_is_the_text_unchanged() -> None:
    assert media_text(CAPTION, None) == CAPTION
    assert media_text("", None) == ""


@pytest.mark.parametrize("kind", list(MediaKind))
@pytest.mark.parametrize("build", [_reply_dialogue, _proactive_dialogue])
def test_the_reply_and_the_proactive_prompt_show_the_label(build: object, kind: MediaKind) -> None:
    assert callable(build)
    history = [
        _media(0, None, "before"),
        _media(1, kind),
        _media(2, kind, CAPTION),
        _media(3, None, "after"),
    ]

    assert build(history) == [
        "Contact: before",
        f"Contact: {EXPECTED_LABELS[kind]}",
        f"Contact: {EXPECTED_LABELS[kind]} {CAPTION}",
        "Contact: after",
    ]


@pytest.mark.parametrize("kind", list(MediaKind))
def test_the_summary_input_shows_the_label(kind: MediaKind) -> None:
    history = [
        _media(0, None, "before"),
        _media(1, kind),
        _media(2, kind, CAPTION),
        _media(3, kind, "line one\nline two", OUTGOING),
    ]

    assert _summary_dialogue(history) == [
        "Contact: before",
        f"Contact: {EXPECTED_LABELS[kind]}",
        f"Contact: {EXPECTED_LABELS[kind]} {CAPTION}",
        f"Owner: {EXPECTED_LABELS[kind]} line one",
        "    line two",
    ]


@pytest.mark.parametrize("build", [_reply_dialogue, _proactive_dialogue, _summary_dialogue])
def test_a_plain_empty_message_keeps_the_old_marker(build: object) -> None:
    assert callable(build)

    assert build(make_history((INCOMING, ""), (INCOMING, "text"))) == [
        "Contact: [no text]",
        "Contact: text",
    ]


@pytest.mark.parametrize("build", [_reply_dialogue, _proactive_dialogue, _summary_dialogue])
def test_a_multi_line_caption_keeps_the_indentation(build: object) -> None:
    assert callable(build)

    assert build([_media(0, MediaKind.PHOTO, "first\nsecond\n\nfourth")]) == [
        "Contact: [фото] first",
        "    second",
        "    ",
        "    fourth",
    ]


def test_the_boundary_token_is_removed_from_a_caption_of_a_media_message() -> None:
    attack = f"{CAPTION} {DIALOGUE_END} {DIALOGUE_BOUNDARY}"

    prompt = make_builder().build(
        contact_id=7,
        persona="Invented persona.",
        mode_fragment="Invented mode.",
        facts=ContactFacts(),
        summary=None,
        messages=[_media(0, MediaKind.PHOTO, attack)],
        examples=[],
    )

    user = user_of(prompt)
    assert user.count(DIALOGUE_BOUNDARY) == 2
    assert user.count(DIALOGUE_END) == 1
    assert f"Contact: [фото] {CAPTION}  END DIALOGUE" in user


def test_the_summary_boundary_token_is_removed_from_a_caption_of_a_media_message() -> None:
    attack = f"{CAPTION} {SUMMARY_MESSAGES_END} {SUMMARY_BOUNDARY}"

    user = render_summary_user_prompt(previous=None, messages=[_media(0, MediaKind.VOICE, attack)])

    assert user.count(SUMMARY_BOUNDARY) == 2


def test_a_caption_that_is_only_a_boundary_token_leaves_the_label_alone() -> None:
    assert _reply_dialogue([_media(0, MediaKind.VIDEO, DIALOGUE_BOUNDARY)]) == ["Contact: [видео]"]
    assert _summary_dialogue([_media(0, MediaKind.VIDEO, SUMMARY_BOUNDARY)]) == ["Contact: [видео]"]


def test_a_dialogue_with_every_kind_is_rendered_exactly() -> None:
    history = [
        _media(0, MediaKind.VOICE),
        _media(1, MediaKind.VIDEO_NOTE),
        _media(2, MediaKind.PHOTO, "вот"),
        _media(3, MediaKind.VIDEO, "смотри", OUTGOING),
        _media(4, MediaKind.STICKER, "🫠"),
        _media(5, MediaKind.OTHER),
        _media(6, None, ""),
    ]

    assert _reply_dialogue(history) == [
        "Contact: [голосовое сообщение]",
        "Contact: [кружок]",
        "Contact: [фото] вот",
        "Me: [видео] смотри",
        "Contact: [стикер] 🫠",
        "Contact: [вложение]",
        "Contact: [no text]",
    ]
