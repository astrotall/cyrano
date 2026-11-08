from __future__ import annotations

import pytest

from cyrano.bot import messages
from cyrano.bot.callbacks import (
    GENDER_CALLBACK_PREFIX,
    CardAction,
    CardCallback,
    GenderAction,
    GenderCallback,
)
from cyrano.bot.rendering import TELEGRAM_TEXT_LIMIT, alert_text, card_keyboard, render_card
from cyrano.domain.enums import CardOutcome, ContactGender, ContactStatus, GenderSource
from cyrano.domain.gender import UNRESOLVED_GENDER, ResolvedGender
from cyrano.domain.models import MAX_BIGINT, ModeChoice
from tests.services.fakes import fake_card

CALLBACK_DATA_LIMIT_BYTES = 64
FEMALE = ContactGender.FEMALE
MALE = ContactGender.MALE
CHECK = messages.GENDER_CHECK_MARK


def _resolved(value: ContactGender | None, source: GenderSource) -> ResolvedGender:
    return ResolvedGender(value=value, source=source)


@pytest.mark.parametrize(
    ("gender", "line"),
    [
        (_resolved(FEMALE, GenderSource.NAME), "Пол: девушка (по имени)"),
        (_resolved(MALE, GenderSource.OWNER), "Пол: парень (выбран вами)"),
        (_resolved(FEMALE, GenderSource.SUMMARY), "Пол: девушка (из переписки)"),
        (_resolved(FEMALE, GenderSource.DEFAULT), "Пол: девушка (по умолчанию)"),
        (_resolved(MALE, GenderSource.NAME), "Пол: парень (по имени)"),
        (_resolved(MALE, GenderSource.SUMMARY), "Пол: парень (из переписки)"),
        (_resolved(MALE, GenderSource.DEFAULT), "Пол: парень (по умолчанию)"),
        (_resolved(FEMALE, GenderSource.OWNER), "Пол: девушка (выбран вами)"),
        (UNRESOLVED_GENDER, "Пол: не определён"),
    ],
)
def test_the_card_shows_one_gender_line_for_each_value_and_source(
    gender: ResolvedGender, line: str
) -> None:
    text = render_card(fake_card(gender=gender))

    assert text.splitlines().count(line) == 1
    assert text.count("Пол:") == 1


def test_the_gender_line_sits_in_the_identity_section() -> None:
    text = render_card(fake_card(gender=_resolved(FEMALE, GenderSource.NAME)))

    identity = text.split("\n\n")[0]
    assert "Пол: девушка (по имени)" in identity


def test_a_pending_or_new_card_shows_no_status_line() -> None:
    for status in (ContactStatus.NEW, ContactStatus.PENDING):
        assert messages.LABEL_STATUS not in render_card(fake_card(status=status))


@pytest.mark.parametrize(
    "status", [ContactStatus.ACTIVE, ContactStatus.PAUSED, ContactStatus.IGNORED]
)
def test_a_decided_contact_shows_its_current_status(status: ContactStatus) -> None:
    text = render_card(fake_card(status=status))

    assert f"{messages.LABEL_STATUS}: {messages.STATUS_NAMES[status]}" in text.split("\n\n")[0]


def _rows(
    card_gender: ResolvedGender, status: ContactStatus = ContactStatus.PENDING
) -> list[list[str]]:
    keyboard = card_keyboard(fake_card(gender=card_gender, status=status))
    return [[button.text for button in row] for row in keyboard.inline_keyboard]


def test_the_first_row_has_the_two_gender_buttons() -> None:
    rows = _rows(UNRESOLVED_GENDER)

    assert rows[0] == [messages.GENDER_FEMALE_BUTTON, messages.GENDER_MALE_BUTTON]
    assert rows[0] == ["Девушка", "Парень"]


@pytest.mark.parametrize(
    ("gender", "expected"),
    [
        (_resolved(FEMALE, GenderSource.OWNER), [f"{CHECK}Девушка", "Парень"]),
        (_resolved(FEMALE, GenderSource.NAME), [f"{CHECK}Девушка", "Парень"]),
        (_resolved(FEMALE, GenderSource.SUMMARY), [f"{CHECK}Девушка", "Парень"]),
        (_resolved(FEMALE, GenderSource.DEFAULT), [f"{CHECK}Девушка", "Парень"]),
        (_resolved(MALE, GenderSource.OWNER), ["Девушка", f"{CHECK}Парень"]),
        (_resolved(MALE, GenderSource.NAME), ["Девушка", f"{CHECK}Парень"]),
        (UNRESOLVED_GENDER, ["Девушка", "Парень"]),
    ],
)
def test_the_check_mark_marks_the_resolved_value_whatever_the_source(
    gender: ResolvedGender, expected: list[str]
) -> None:
    assert _rows(gender)[0] == expected


def test_with_a_known_gender_the_reply_buttons_follow_and_ignore_is_last() -> None:
    rows = _rows(_resolved(FEMALE, GenderSource.NAME))

    assert rows[1:] == [
        [messages.REPLY_BUTTON.format(mode="default")],
        [messages.REPLY_BUTTON.format(mode="flirt")],
        [messages.IGNORE_BUTTON],
    ]


@pytest.mark.parametrize("status", [ContactStatus.NEW, ContactStatus.PENDING])
def test_with_an_unknown_gender_there_are_no_reply_buttons_but_ignore_stays(
    status: ContactStatus,
) -> None:
    rows = _rows(UNRESOLVED_GENDER, status)

    assert rows[1:] == [[messages.IGNORE_BUTTON]]


def test_ignore_is_there_for_every_known_source_even_without_modes() -> None:
    keyboard = card_keyboard(fake_card(gender=_resolved(MALE, GenderSource.OWNER), modes=[]))

    assert [b.text for row in keyboard.inline_keyboard[1:] for b in row] == [messages.IGNORE_BUTTON]


@pytest.mark.parametrize(
    "status", [ContactStatus.ACTIVE, ContactStatus.PAUSED, ContactStatus.IGNORED]
)
@pytest.mark.parametrize("gender", [UNRESOLVED_GENDER, _resolved(FEMALE, GenderSource.OWNER)])
def test_a_decided_contact_keeps_only_the_gender_row(
    status: ContactStatus, gender: ResolvedGender
) -> None:
    rows = _rows(gender, status)

    assert len(rows) == 1
    assert len(rows[0]) == 2


def test_the_buttons_carry_the_gender_callbacks_of_the_card_contact() -> None:
    keyboard = card_keyboard(fake_card(contact_id=17, gender=UNRESOLVED_GENDER))

    first_row = keyboard.inline_keyboard[0]
    assert [GenderCallback.unpack(str(b.callback_data)) for b in first_row] == [
        GenderCallback(action=GenderAction.FEMALE, contact_id=17),
        GenderCallback(action=GenderAction.MALE, contact_id=17),
    ]


def test_the_other_buttons_keep_their_callbacks() -> None:
    keyboard = card_keyboard(fake_card(contact_id=17, gender=_resolved(MALE, GenderSource.NAME)))

    buttons = [b for row in keyboard.inline_keyboard[1:] for b in row]
    assert [CardCallback.unpack(str(b.callback_data)) for b in buttons] == [
        CardCallback(action=CardAction.REPLY, contact_id=17, mode_id=1),
        CardCallback(action=CardAction.REPLY, contact_id=17, mode_id=2),
        CardCallback(action=CardAction.IGNORE, contact_id=17),
    ]


def test_the_gender_callback_has_its_own_prefix_and_round_trips() -> None:
    packed = GenderCallback(action=GenderAction.MALE, contact_id=5).pack()

    assert packed.startswith(f"{GENDER_CALLBACK_PREFIX}:")
    assert GenderCallback.unpack(packed) == GenderCallback(action=GenderAction.MALE, contact_id=5)
    assert not packed.startswith("card:")
    assert not packed.startswith("pause:")


@pytest.mark.parametrize("action", list(GenderAction))
def test_the_gender_callback_data_stays_below_the_telegram_limit(action: GenderAction) -> None:
    callback = GenderCallback(action=action, contact_id=MAX_BIGINT)

    assert len(callback.pack().encode()) < CALLBACK_DATA_LIMIT_BYTES


def test_the_gender_required_alert_asks_for_the_gender() -> None:
    assert alert_text(CardOutcome.GENDER_REQUIRED) == messages.ALERT_GENDER_REQUIRED
    assert "пол" in messages.ALERT_GENDER_REQUIRED.lower()


def test_a_card_with_all_sections_stays_under_the_telegram_limit_with_the_new_lines() -> None:
    card = fake_card(
        display_name="а" * 100,
        bio="б" * 400,
        status=ContactStatus.PAUSED,
        gender=_resolved(FEMALE, GenderSource.SUMMARY),
        modes=[ModeChoice(id=n, name="м" * 40) for n in range(1, 20)],
    )

    assert len(render_card(card)) < TELEGRAM_TEXT_LIMIT
