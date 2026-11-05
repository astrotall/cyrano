from __future__ import annotations

from datetime import UTC, datetime

from cyrano.domain.enums import ContactStatus, LeomatchNameSource, MessageDirection
from cyrano.domain.models import (
    CARD_BIO_LENGTH,
    CARD_LEOMATCH_RAW_LENGTH,
    CARD_MESSAGE_LENGTH,
    CARD_MODE_NAME_LENGTH,
    CARD_NAME_LENGTH,
    MAX_BIO_LENGTH,
    MAX_LEOMATCH_TEXT_LENGTH,
    MAX_USERNAME_LENGTH,
    Contact,
    ContactProfile,
    LeomatchProfile,
    Message,
    Mode,
    ModeChoice,
)
from cyrano.services.card_builder import build_card
from cyrano.services.sanitize import NUL

NOW = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)


def _contact(profile: dict[str, object] | None = None, **overrides: object) -> Contact:
    fields: dict[str, object] = {
        "id": 17,
        "tg_id": 5001,
        "username": "fake_user",
        "display_name": "Fakefirst Fakelast",
        "status": ContactStatus.NEW,
        "pause_reason": None,
        "mode_id": None,
        "profile": profile if profile is not None else {},
        "summary": {},
        "last_message_at": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return Contact.model_validate({**fields, **overrides})


def _message(
    text: str, *, direction: MessageDirection = MessageDirection.INCOMING, number: int = 1
) -> Message:
    return Message(
        id=number,
        contact_id=17,
        direction=direction,
        text=text,
        tg_message_id=number,
        created_at=NOW,
    )


def _mode(mode_id: int, name: str, *, enabled: bool = True) -> Mode:
    return Mode(
        id=mode_id,
        name=name,
        prompt_fragment="",
        few_shot_examples=[],
        enabled=enabled,
        created_at=NOW,
        updated_at=NOW,
    )


def _profile(**fields: object) -> dict[str, object]:
    return ContactProfile.model_validate(fields).model_dump(mode="json")


def test_a_contact_without_a_profile_gives_a_card_that_says_nothing_was_collected() -> None:
    card = build_card(_contact(), [], [])

    assert card.contact_id == 17
    assert (card.display_name, card.username) == ("Fakefirst Fakelast", "fake_user")
    assert card.profile_collected is False
    assert (card.bio, card.leomatch, card.profile_photo, card.card_photos) == (
        None,
        None,
        None,
        [],
    )
    assert (card.messages, card.modes) == ([], [])


def test_a_collected_profile_fills_the_card() -> None:
    profile = _profile(
        bio="fake bio",
        photo="profiles/17.jpg",
        leomatch=LeomatchProfile(
            raw_text="raw",
            name="Anya",
            age=27,
            city="Town",
            about="about",
            name_source=LeomatchNameSource.CARD,
            photos=["profiles/17-card-1.jpg", "profiles/17-card-2.jpg"],
        ),
        collected_at=NOW,
    )

    card = build_card(_contact(profile), [], [])

    assert card.profile_collected is True
    assert card.bio == "fake bio"
    assert card.profile_photo == "profiles/17.jpg"
    assert card.card_photos == ["profiles/17-card-1.jpg", "profiles/17-card-2.jpg"]
    assert card.leomatch is not None
    assert (card.leomatch.name, card.leomatch.age, card.leomatch.city) == ("Anya", 27, "Town")
    assert (card.leomatch.about, card.leomatch.raw_text) == ("about", "raw")
    assert card.leomatch.name_source is LeomatchNameSource.CARD


def test_identity_falls_back_to_the_profile_when_the_contact_has_none() -> None:
    profile = _profile(username="from_profile", display_name="Profile Name", collected_at=NOW)

    card = build_card(_contact(profile, username=None, display_name=None), [], [])

    assert (card.display_name, card.username) == ("Profile Name", "from_profile")


def test_a_profile_that_does_not_validate_is_treated_as_empty() -> None:
    card = build_card(_contact({"photo": 5, "collected_at": "not a date"}), [], [])

    assert card.profile_collected is False
    assert card.profile_photo is None


def test_every_text_is_truncated_to_its_named_limit_and_stripped_of_nul_bytes() -> None:
    long = "x" * 5000
    profile = _profile(
        bio=long[:MAX_BIO_LENGTH],
        leomatch=LeomatchProfile(raw_text=long[:MAX_LEOMATCH_TEXT_LENGTH]),
        collected_at=NOW,
    )
    contact = _contact(profile, username=f"{NUL}{long}", display_name=f"{NUL}{long}")

    card = build_card(contact, [_message(f"{NUL}{long}")], [_mode(1, f"{long}")])

    assert len(card.display_name or "") == CARD_NAME_LENGTH
    assert len(card.username or "") == MAX_USERNAME_LENGTH
    assert len(card.bio or "") == CARD_BIO_LENGTH
    assert card.leomatch is not None
    assert len(card.leomatch.raw_text or "") == CARD_LEOMATCH_RAW_LENGTH
    assert [len(t) for t in card.messages] == [CARD_MESSAGE_LENGTH]
    assert [len(m.name) for m in card.modes] == [CARD_MODE_NAME_LENGTH]
    assert NUL not in card.model_dump_json()


def test_only_incoming_messages_with_text_are_shown_in_the_order_given() -> None:
    messages = [
        _message("first", number=1),
        _message("mine", direction=MessageDirection.OUTGOING, number=2),
        _message("", number=3),
        _message("  third  ", number=4),
    ]

    card = build_card(_contact(), messages, [])

    assert card.messages == ["first", "third"]


def test_only_enabled_modes_become_choices_in_the_given_order() -> None:
    modes = [_mode(1, "default"), _mode(2, "retired", enabled=False), _mode(3, "flirt")]

    card = build_card(_contact(), [], modes)

    assert card.modes == [ModeChoice(id=1, name="default"), ModeChoice(id=3, name="flirt")]


def test_a_blank_card_text_gives_a_leomatch_part_without_card_text() -> None:
    profile = _profile(leomatch=LeomatchProfile(raw_text="   ", match_text="m"), collected_at=NOW)

    card = build_card(_contact(profile), [], [])

    assert card.leomatch is not None
    assert card.leomatch.raw_text is None


def test_the_match_text_never_reaches_the_card() -> None:
    profile = _profile(
        leomatch=LeomatchProfile(match_text="match-text-secret", name="Linkname"),
        collected_at=NOW,
    )

    card = build_card(_contact(profile), [], [])

    assert card.leomatch is not None
    assert card.leomatch.raw_text is None
    assert "match-text-secret" not in card.model_dump_json()


def test_a_fallback_name_comes_with_its_source() -> None:
    profile = _profile(
        leomatch=LeomatchProfile(
            match_text="m", name="Linkname", name_source=LeomatchNameSource.MATCH
        ),
        collected_at=NOW,
    )

    card = build_card(_contact(profile), [], [])

    assert card.leomatch is not None
    assert (card.leomatch.name, card.leomatch.name_source) == (
        "Linkname",
        LeomatchNameSource.MATCH,
    )


def test_the_card_photos_are_reported_even_without_a_card_text() -> None:
    profile = _profile(
        leomatch=LeomatchProfile(match_text="m", photos=["profiles/17-card-1.jpg"]),
        collected_at=NOW,
    )

    assert build_card(_contact(profile), [], []).card_photos == ["profiles/17-card-1.jpg"]


def test_a_legacy_single_card_photo_reaches_the_card() -> None:
    legacy: dict[str, object] = {"leomatch": {"raw_text": "text", "photo": "profiles/17-card.jpg"}}

    card = build_card(_contact(legacy), [], [])

    assert card.card_photos == ["profiles/17-card.jpg"]
