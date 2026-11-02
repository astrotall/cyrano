from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cyrano.domain.enums import TurnRole
from cyrano.domain.style import (
    LAST_TURN_NOT_ME_RULE,
    MAX_PERSONA_LENGTH,
    MAX_TAG_LENGTH,
    MAX_TAGS_PER_DIALOGUE,
    MAX_TURN_LENGTH,
    MAX_TURNS_PER_DIALOGUE,
    NO_ME_TURN_RULE,
    Persona,
    PersonaCreate,
    StyleExampleCreate,
    Turn,
    content_hash,
)

MARKER = "zq-private-marker"


def _turns(*roles: str) -> list[dict[str, str]]:
    return [{"role": role, "text": f"line {index}"} for index, role in enumerate(roles)]


def test_a_turn_is_trimmed_and_loses_nul_bytes() -> None:
    turn = Turn.model_validate({"role": "me", "text": "  hel\x00lo \n"})

    assert turn.text == "hello"
    assert turn.role is TurnRole.ME


@pytest.mark.parametrize("text", ["", "   ", "\x00", " \x00 \n"])
def test_a_turn_that_is_empty_after_cleaning_is_rejected(text: str) -> None:
    with pytest.raises(ValidationError):
        Turn.model_validate({"role": "me", "text": text})


def test_a_turn_at_the_limit_passes_and_one_over_fails() -> None:
    Turn.model_validate({"role": "me", "text": "x" * MAX_TURN_LENGTH})

    with pytest.raises(ValidationError):
        Turn.model_validate({"role": "me", "text": "x" * (MAX_TURN_LENGTH + 1)})


def test_an_unknown_role_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Turn.model_validate({"role": "bot", "text": "hi"})


def test_tags_are_lowercased_deduplicated_and_sorted() -> None:
    example = StyleExampleCreate.model_validate(
        {"tags": ["Flirty", " default ", "FLIRTY", "a_b-c"], "turns": _turns("them", "me")}
    )

    assert example.tags == ["a_b-c", "default", "flirty"]


def test_missing_tags_mean_generic() -> None:
    assert StyleExampleCreate.model_validate({"turns": _turns("me")}).tags == []


@pytest.mark.parametrize(
    "tag", ["", "  ", "two words", "dot.ted", "x" * (MAX_TAG_LENGTH + 1), "a/b"]
)
def test_invalid_tags_are_rejected(tag: str) -> None:
    with pytest.raises(ValidationError):
        StyleExampleCreate.model_validate({"tags": [tag], "turns": _turns("me")})


def test_a_tag_at_the_length_limit_and_unicode_letters_pass() -> None:
    example = StyleExampleCreate.model_validate(
        {"tags": ["y" * MAX_TAG_LENGTH, "Флирт"], "turns": _turns("me")}
    )

    assert example.tags == ["y" * MAX_TAG_LENGTH, "флирт"]


def test_too_many_tags_are_rejected() -> None:
    tags = [f"tag{index}" for index in range(MAX_TAGS_PER_DIALOGUE + 1)]

    with pytest.raises(ValidationError):
        StyleExampleCreate.model_validate({"tags": tags, "turns": _turns("me")})


def test_turn_count_limits() -> None:
    StyleExampleCreate.model_validate(
        {"turns": _turns(*["them", "me"] * (MAX_TURNS_PER_DIALOGUE // 2))}
    )

    with pytest.raises(ValidationError):
        StyleExampleCreate.model_validate(
            {"turns": _turns(*["them"] * MAX_TURNS_PER_DIALOGUE, "me")}
        )
    with pytest.raises(ValidationError):
        StyleExampleCreate.model_validate({"turns": []})


def test_the_last_turn_must_be_mine() -> None:
    with pytest.raises(ValidationError, match=LAST_TURN_NOT_ME_RULE):
        StyleExampleCreate.model_validate({"turns": _turns("me", "them")})


def test_a_dialogue_without_my_turn_is_rejected() -> None:
    with pytest.raises(ValidationError, match=NO_ME_TURN_RULE):
        StyleExampleCreate.model_validate({"turns": _turns("them", "them")})


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        StyleExampleCreate.model_validate({"turns": _turns("me"), "extra": 1})


def test_validation_errors_never_contain_the_input_text() -> None:
    cases: list[Callable[[], object]] = [
        lambda: Turn.model_validate({"role": "me", "text": MARKER * 500}),
        lambda: Turn.model_validate({"role": MARKER, "text": "hi"}),
        lambda: StyleExampleCreate.model_validate({"tags": [MARKER + " x"], "turns": _turns("me")}),
        lambda: StyleExampleCreate.model_validate({"turns": [{"role": "them", "text": MARKER}]}),
        lambda: PersonaCreate.model_validate({"text": MARKER * 2000}),
    ]

    for case in cases:
        with pytest.raises(ValidationError) as caught:
            case()
        assert MARKER not in str(caught.value)
        assert MARKER not in repr(caught.value.errors(include_input=False))


def test_persona_text_is_cleaned_and_limited() -> None:
    assert PersonaCreate(text=" a\x00b \n").text == "ab"
    PersonaCreate(text="x" * MAX_PERSONA_LENGTH)

    with pytest.raises(ValidationError):
        PersonaCreate(text="x" * (MAX_PERSONA_LENGTH + 1))
    with pytest.raises(ValidationError):
        PersonaCreate(text="  \n ")


def test_a_stored_persona_requires_an_aware_time_and_normalizes_it_to_utc() -> None:
    with pytest.raises(ValidationError):
        Persona(text="x", updated_at=datetime(2025, 1, 1))

    persona = Persona(text="x", updated_at=datetime(2025, 1, 1, 12, tzinfo=UTC))

    assert persona.updated_at.utcoffset() is not None
    assert persona.updated_at.tzinfo is UTC


def test_the_content_hash_ignores_tag_order_and_case_but_not_text_or_role() -> None:
    base = StyleExampleCreate.model_validate({"tags": ["a", "b"], "turns": _turns("them", "me")})
    reordered = StyleExampleCreate.model_validate(
        {"tags": ["B", "a", "a"], "turns": _turns("them", "me")}
    )
    other_text = StyleExampleCreate.model_validate(
        {"tags": ["a", "b"], "turns": [*_turns("them")[:1], {"role": "me", "text": "changed"}]}
    )
    other_role = StyleExampleCreate.model_validate(
        {"tags": ["a", "b"], "turns": _turns("me", "me")}
    )
    other_tags = StyleExampleCreate.model_validate({"tags": ["a"], "turns": _turns("them", "me")})

    assert base.content_hash == reordered.content_hash
    assert (
        len(
            {
                base.content_hash,
                other_text.content_hash,
                other_role.content_hash,
                other_tags.content_hash,
            }
        )
        == 4
    )
    assert len(base.content_hash) == 64
    assert base.content_hash == content_hash(base.tags, base.turns)
