from __future__ import annotations

from collections.abc import Sequence

from cyrano.domain.enums import (
    BlockKind,
    ContactGender,
    MediaKind,
    MessageDirection,
    ReplyPurpose,
    WrapUpPhase,
)
from cyrano.domain.models import Message
from cyrano.domain.reply import BuiltPrompt, ContactFacts, MessageRef
from cyrano.domain.schedule import UnavailableSoon
from cyrano.domain.summary import ContactSummary
from cyrano.prompts.render import render_summary_layer, render_summary_user_prompt
from cyrano.prompts.texts import (
    CONTINUATION_INDENT,
    DIALOGUE_BEGIN,
    DIALOGUE_END,
    INSTRUCTIONS,
    LAYER_SEPARATOR,
    PROACTIVE_INSTRUCTIONS,
    QUOTE_INSTRUCTIONS,
)
from tests.reply_data import (
    FIXED_NOW,
    INCOMING,
    OUTGOING,
    make_builder,
    make_example,
    make_history,
    make_message,
    system_of,
    user_of,
)
from tests.services.test_prompt_snapshot import EXPECTED_SYSTEM, EXPECTED_USER

EXPECTED_PARAGRAPH = (
    "The messages of the dialogue are numbered. When one of your lines answers a specific earlier "
    "message of the contact instead of the latest one, start that line with the marker ↩N, where "
    "N is the number of that message. Use the marker only when it helps, for example when several "
    "messages of the contact are waiting and the line answers one that is not the last; use it at "
    "most once per reply and never for a plain answer to the latest message."
)
SNAPSHOT_PERSONA = (
    "Invented persona: Alex, 31, cycles to work, texts in short lowercase lines, rarely uses emoji."
)
SNAPSHOT_MODE = "Keep it light and playful. Ask one easy question when the chat slows down."


def _snapshot_messages() -> list[Message]:
    return make_history(
        (INCOMING, "hi! are you free this weekend?"),
        (OUTGOING, "maybe, what do you have in mind"),
        (INCOMING, "a walk in the park,\nthen tea somewhere warm"),
    )


def _snapshot_prompt(*, quote_enabled: bool, purpose: ReplyPurpose) -> BuiltPrompt:
    return make_builder(quote_enabled=quote_enabled).build(
        contact_id=7,
        persona=SNAPSHOT_PERSONA,
        mode_fragment=SNAPSHOT_MODE,
        facts=ContactFacts(
            name="Sasha",
            gender=ContactGender.FEMALE,
            age=28,
            city="Inventedtown",
            about="loves tea and long walks",
            bio="nothing to see here",
        ),
        summary=render_summary_layer(
            ContactSummary(
                name="Саша",
                city="Инвентград",
                interests=["велосипед", "чай"],
                agreements_and_plans=["прогулка в выходные"],
                open_questions=["какой парк"],
                told_about_owner=["владелец ездит на работу на велосипеде"],
            )
        ),
        messages=_snapshot_messages(),
        examples=[
            make_example(
                ("them", "how was your week?"),
                ("me", "busy, but i got out on the bike on sunday"),
            ),
            make_example(("them", "do you cook?\nhonestly asking"), ("me", "on sundays, yes")),
        ],
        purpose=purpose,
    )


def _build(
    messages: Sequence[Message],
    *,
    quote_enabled: bool = True,
    budget: int = 1_000_000,
    purpose: ReplyPurpose = ReplyPurpose.REPLY,
    wrapup: UnavailableSoon | None = None,
) -> BuiltPrompt:
    return make_builder(budget, quote_enabled=quote_enabled).build(
        contact_id=7,
        persona="Invented persona.",
        mode_fragment="",
        facts=ContactFacts(),
        summary=None,
        messages=messages,
        examples=[],
        purpose=purpose,
        wrapup=wrapup,
    )


def _dialogue(prompt: BuiltPrompt) -> list[str]:
    lines = user_of(prompt).splitlines()
    return lines[lines.index(DIALOGUE_BEGIN) + 1 : lines.index(DIALOGUE_END)]


def test_the_paragraph_is_the_text_of_the_ticket() -> None:
    assert QUOTE_INSTRUCTIONS == EXPECTED_PARAGRAPH


def test_without_the_feature_the_prompt_is_byte_for_byte_the_snapshot() -> None:
    prompt = _snapshot_prompt(quote_enabled=False, purpose=ReplyPurpose.REPLY)

    assert system_of(prompt) == EXPECTED_SYSTEM
    assert user_of(prompt) == EXPECTED_USER
    assert prompt.message_refs == {}


def test_with_the_feature_only_the_paragraph_and_the_numbers_are_added() -> None:
    prompt = _snapshot_prompt(quote_enabled=True, purpose=ReplyPurpose.REPLY)

    assert system_of(prompt) == EXPECTED_SYSTEM.replace(
        INSTRUCTIONS, f"{INSTRUCTIONS}{LAYER_SEPARATOR}{EXPECTED_PARAGRAPH}", 1
    )
    assert user_of(prompt) == EXPECTED_USER.replace(
        "Contact: hi! are you free", "#1 Contact: hi! are you free"
    ).replace("Me: maybe", "#2 Me: maybe").replace("Contact: a walk", "#3 Contact: a walk")


def test_the_paragraph_follows_the_bullets_after_a_blank_line() -> None:
    system = system_of(_build(make_history((INCOMING, "hi"))))

    assert f"{INSTRUCTIONS}{LAYER_SEPARATOR}{EXPECTED_PARAGRAPH}" in system
    assert system.count(EXPECTED_PARAGRAPH) == 1


def test_the_proactive_prompt_is_unchanged_with_and_without_the_feature() -> None:
    without = _snapshot_prompt(quote_enabled=False, purpose=ReplyPurpose.PROACTIVE)
    with_feature = _snapshot_prompt(quote_enabled=True, purpose=ReplyPurpose.PROACTIVE)

    assert with_feature.messages == without.messages
    assert with_feature.message_refs == {}
    assert EXPECTED_PARAGRAPH not in system_of(with_feature)
    assert "#1 " not in user_of(with_feature)
    assert PROACTIVE_INSTRUCTIONS.split("{")[0] in system_of(with_feature)


def test_every_line_carries_its_number_counted_from_the_oldest_shown() -> None:
    prompt = _build(
        make_history(
            (INCOMING, "first invented"),
            (OUTGOING, "second invented"),
            (INCOMING, "third invented"),
            (INCOMING, "fourth invented"),
        )
    )

    assert _dialogue(prompt) == [
        "#1 Contact: first invented",
        "#2 Me: second invented",
        "#3 Contact: third invented",
        "#4 Contact: fourth invented",
    ]


def test_the_map_holds_the_telegram_id_and_the_direction_of_every_number() -> None:
    messages = make_history(
        (INCOMING, "first invented"), (OUTGOING, "second invented"), (INCOMING, "third")
    )

    prompt = _build(messages)

    assert prompt.message_refs == {
        1: MessageRef(tg_message_id=messages[0].tg_message_id, direction=INCOMING),
        2: MessageRef(tg_message_id=messages[1].tg_message_id, direction=OUTGOING),
        3: MessageRef(tg_message_id=messages[2].tg_message_id, direction=INCOMING),
    }


def test_a_multi_line_message_keeps_its_continuation_indent() -> None:
    prompt = _build(make_history((OUTGOING, "earlier"), (INCOMING, "line one\nline two\n\nfour")))

    assert _dialogue(prompt) == [
        "#1 Me: earlier",
        "#2 Contact: line one",
        f"{CONTINUATION_INDENT}line two",
        CONTINUATION_INDENT,
        f"{CONTINUATION_INDENT}four",
    ]
    assert set(prompt.message_refs) == {1, 2}


def test_the_budget_trimming_keeps_the_map_in_step_with_what_is_shown() -> None:
    messages = [
        make_message(index, INCOMING if index % 2 else OUTGOING, f"invented message {index} " * 30)
        for index in range(12)
    ]
    full = _build(messages)
    budget = full.characters - 1500

    prompt = _build(messages, budget=budget)

    shown = messages[len(messages) - prompt.messages_kept :]
    assert 4 <= prompt.messages_kept < len(messages)
    assert sorted(prompt.message_refs) == list(range(1, prompt.messages_kept + 1))
    for number, message in enumerate(shown, start=1):
        assert prompt.message_refs[number] == MessageRef(
            tg_message_id=message.tg_message_id, direction=message.direction
        )
    dialogue = _dialogue(prompt)
    assert dialogue[0].startswith("#1 ")
    assert dialogue[-1].startswith(f"#{prompt.messages_kept} ")
    assert f"invented message {shown[0].id} " in dialogue[0]


def test_a_media_label_is_numbered_like_any_message() -> None:
    sticker = make_message(1, INCOMING, "🫠").model_copy(update={"media_kind": MediaKind.STICKER})
    photo = make_message(2, INCOMING, "").model_copy(update={"media_kind": MediaKind.PHOTO})
    prompt = _build([make_message(0, OUTGOING, "hi"), sticker, photo])

    assert _dialogue(prompt) == ["#1 Me: hi", "#2 Contact: [стикер] 🫠", "#3 Contact: [фото]"]
    assert prompt.message_refs[3].tg_message_id == photo.tg_message_id


def test_the_feature_off_gives_no_numbers_and_no_map() -> None:
    prompt = _build(make_history((INCOMING, "a"), (OUTGOING, "b")), quote_enabled=False)

    assert _dialogue(prompt) == ["Contact: a", "Me: b"]
    assert prompt.message_refs == {}
    assert EXPECTED_PARAGRAPH not in system_of(prompt)


def test_the_wrapup_paragraph_still_comes_after_the_quote_paragraph() -> None:
    soon = UnavailableSoon(
        kind=BlockKind.NIGHT, starts_at=FIXED_NOW, minutes_left=10, phase=WrapUpPhase.HARD
    )

    system = system_of(_build(make_history((INCOMING, "hi")), wrapup=soon))

    assert system.index(EXPECTED_PARAGRAPH) < system.index("The owner becomes unavailable")


def test_the_summary_extraction_input_has_no_numbers() -> None:
    messages = make_history((INCOMING, "invented a"), (OUTGOING, "invented b"))

    user = render_summary_user_prompt(previous=None, messages=messages)

    assert "#1 " not in user
    assert "#2 " not in user


def test_the_map_never_shows_a_telegram_id_in_its_repr() -> None:
    prompt = _build([make_message(0, MessageDirection.INCOMING, "x")])

    assert str(prompt.message_refs[1].tg_message_id) not in repr(prompt)
    assert str(prompt.message_refs[1].tg_message_id) not in repr(prompt.message_refs[1])
