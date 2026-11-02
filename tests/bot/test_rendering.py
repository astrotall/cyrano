from __future__ import annotations

from cyrano.bot import messages
from cyrano.bot.callbacks import CardAction, CardCallback
from cyrano.bot.rendering import (
    TELEGRAM_TEXT_LIMIT,
    alert_text,
    append_decision,
    card_keyboard,
    decision_text,
    render_card,
)
from cyrano.domain.enums import CardOutcome, ContactStatus, LeomatchNameSource
from cyrano.domain.models import (
    CARD_BIO_LENGTH,
    CARD_LEOMATCH_ABOUT_LENGTH,
    CARD_LEOMATCH_RAW_LENGTH,
    CARD_MESSAGE_LENGTH,
    CARD_MESSAGES_LIMIT,
    CARD_NAME_LENGTH,
    MAX_BIGINT,
    CardActionResult,
    CardLeomatch,
    ModeChoice,
)
from tests.services.fakes import fake_card

MARKUP = "<b>x</b>"
ESCAPED_MARKUP = "&lt;b&gt;x&lt;/b&gt;"
CALLBACK_DATA_LIMIT_BYTES = 64


def test_a_name_that_looks_like_markup_is_escaped() -> None:
    text = render_card(fake_card(display_name=MARKUP))

    assert ESCAPED_MARKUP in text
    assert f"Имя: {MARKUP}" not in text


def test_username_bio_messages_and_leomatch_text_are_escaped() -> None:
    card = fake_card(
        username=MARKUP,
        bio=f"bio {MARKUP} & more",
        messages=[f"hello {MARKUP}"],
        leomatch=CardLeomatch(raw_text=f"raw {MARKUP}"),
    )

    text = render_card(card)

    assert MARKUP not in text
    assert text.count(ESCAPED_MARKUP) == 4
    assert "bio &lt;b&gt;x&lt;/b&gt; &amp; more" in text


def test_a_mode_name_that_looks_like_markup_is_escaped_in_the_decision() -> None:
    result = CardActionResult(
        outcome=CardOutcome.APPLIED, status=ContactStatus.ACTIVE, mode_name=MARKUP
    )

    assert ESCAPED_MARKUP in append_decision("card", result)
    assert MARKUP not in append_decision("card", result)


def test_missing_fields_are_omitted() -> None:
    text = render_card(fake_card(display_name=None, username=None))

    assert messages.LABEL_NAME not in text
    assert messages.LABEL_USERNAME not in text
    assert messages.LABEL_BIO not in text
    assert messages.LABEL_MESSAGES not in text
    assert messages.LABEL_ANKETA not in text
    assert messages.LABEL_ANKETA_RAW not in text
    assert text.startswith(f"<b>{messages.CARD_TITLE}</b>")


def test_a_card_with_everything_shows_every_part() -> None:
    card = fake_card(
        bio="fake bio",
        messages=["first", "second"],
        leomatch=CardLeomatch(
            name="Anya",
            name_source=LeomatchNameSource.CARD,
            age=27,
            city="Town",
            about="fake about",
            raw_text="r",
        ),
    )

    text = render_card(card)

    assert "Имя: Fakefirst Fakelast" in text
    assert "Ник: @fake_user" in text
    assert "fake bio" in text
    assert "• first\n• second" in text
    assert "Anya, 27, Town\nfake about" in text
    assert messages.LABEL_ANKETA_RAW not in text


def test_an_unparsed_leomatch_text_is_shown_raw() -> None:
    text = render_card(fake_card(leomatch=CardLeomatch(raw_text="some raw card")))

    assert f"<b>{messages.LABEL_ANKETA_RAW}</b>\nsome raw card" in text


def test_a_leomatch_card_without_age_and_city_shows_the_name_only() -> None:
    text = render_card(
        fake_card(
            leomatch=CardLeomatch(name="Anya", name_source=LeomatchNameSource.CARD, raw_text="r")
        )
    )

    assert f"<b>{messages.LABEL_ANKETA}</b>\nAnya" in text


def test_a_card_that_was_not_found_says_so_and_never_shows_the_match_text() -> None:
    text = render_card(fake_card(leomatch=CardLeomatch()))

    assert f"<b>{messages.LABEL_ANKETA}</b>\n{messages.CARD_NOT_FOUND}" in text
    assert messages.LABEL_ANKETA_RAW not in text
    assert messages.LABEL_NAME + ": None" not in text


def test_a_card_that_was_not_found_shows_the_fallback_name_as_the_name() -> None:
    leomatch = CardLeomatch(name=f"Link {MARKUP}", name_source=LeomatchNameSource.MATCH)

    text = render_card(fake_card(leomatch=leomatch))

    assert f"{messages.CARD_NOT_FOUND}\n{messages.LABEL_NAME}: Link {ESCAPED_MARKUP}" in text
    assert MARKUP not in text


def test_an_unparsed_card_text_comes_with_the_fallback_name_first() -> None:
    leomatch = CardLeomatch(
        name="Linkname", name_source=LeomatchNameSource.MATCH, raw_text="free card text"
    )

    text = render_card(fake_card(leomatch=leomatch))

    assert (
        f"<b>{messages.LABEL_ANKETA_RAW}</b>\n{messages.LABEL_NAME}: Linkname\nfree card text"
        in text
    )
    assert messages.CARD_NOT_FOUND not in text


def test_a_profile_that_was_not_collected_says_so() -> None:
    assert messages.PROFILE_NOT_COLLECTED in render_card(fake_card(profile_collected=False))
    assert messages.PROFILE_NOT_COLLECTED not in render_card(fake_card(profile_collected=True))


def test_a_card_without_enabled_modes_says_so() -> None:
    assert messages.NO_MODES_NOTICE in render_card(fake_card(modes=[]))
    assert messages.NO_MODES_NOTICE not in render_card(fake_card())


def test_the_largest_card_stays_below_the_telegram_limit() -> None:
    card = fake_card(
        display_name="n" * CARD_NAME_LENGTH,
        username="u" * 64,
        bio="b" * CARD_BIO_LENGTH,
        messages=["m" * CARD_MESSAGE_LENGTH] * CARD_MESSAGES_LIMIT,
        leomatch=CardLeomatch(
            name="a" * 100,
            name_source=LeomatchNameSource.CARD,
            age=99,
            city="c" * 100,
            about="o" * CARD_LEOMATCH_ABOUT_LENGTH,
            raw_text="r" * CARD_LEOMATCH_RAW_LENGTH,
        ),
        profile_collected=False,
    )

    assert len(render_card(card)) < TELEGRAM_TEXT_LIMIT


def test_a_card_made_only_of_characters_that_grow_when_escaped_stays_below_the_limit() -> None:
    growing = "&" * CARD_MESSAGE_LENGTH
    card = fake_card(
        display_name="&" * CARD_NAME_LENGTH,
        username="u" * 64,
        bio="&" * CARD_BIO_LENGTH,
        messages=[growing] * CARD_MESSAGES_LIMIT,
        leomatch=CardLeomatch(
            name="&" * 100,
            name_source=LeomatchNameSource.CARD,
            age=99,
            city="&" * 100,
            about="&" * CARD_LEOMATCH_ABOUT_LENGTH,
            raw_text="&" * CARD_LEOMATCH_RAW_LENGTH,
        ),
        profile_collected=False,
    )

    text = render_card(card)

    assert len(text) < TELEGRAM_TEXT_LIMIT
    assert text.startswith(f"<b>{messages.CARD_TITLE}</b>")
    assert "&amp;" in text
    assert "&am<" not in text


def test_the_keyboard_has_one_button_per_enabled_mode_and_an_ignore_button() -> None:
    keyboard = card_keyboard(fake_card())

    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert [b.text for b in buttons] == [
        messages.REPLY_BUTTON.format(mode="default"),
        messages.REPLY_BUTTON.format(mode="flirt"),
        messages.IGNORE_BUTTON,
    ]
    assert [CardCallback.unpack(str(b.callback_data)) for b in buttons] == [
        CardCallback(action=CardAction.REPLY, contact_id=17, mode_id=1),
        CardCallback(action=CardAction.REPLY, contact_id=17, mode_id=2),
        CardCallback(action=CardAction.IGNORE, contact_id=17),
    ]


def test_a_disabled_mode_is_absent_because_the_card_lists_enabled_modes_only() -> None:
    keyboard = card_keyboard(fake_card(modes=[ModeChoice(id=2, name="flirt")]))

    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert texts == [messages.REPLY_BUTTON.format(mode="flirt"), messages.IGNORE_BUTTON]


def test_a_card_without_modes_has_only_the_ignore_button() -> None:
    keyboard = card_keyboard(fake_card(modes=[]))

    assert [b.text for row in keyboard.inline_keyboard for b in row] == [messages.IGNORE_BUTTON]


def test_callback_data_stays_far_below_the_telegram_limit() -> None:
    largest = [
        CardCallback(action=CardAction.REPLY, contact_id=MAX_BIGINT, mode_id=MAX_BIGINT),
        CardCallback(action=CardAction.IGNORE, contact_id=MAX_BIGINT),
    ]

    for callback in largest:
        assert len(callback.pack().encode()) < CALLBACK_DATA_LIMIT_BYTES


def test_callback_data_round_trips() -> None:
    packed = CardCallback(action=CardAction.REPLY, contact_id=5, mode_id=3).pack()

    assert CardCallback.unpack(packed) == CardCallback(
        action=CardAction.REPLY, contact_id=5, mode_id=3
    )


def test_decision_text_for_each_final_state() -> None:
    active = CardActionResult(
        outcome=CardOutcome.APPLIED, status=ContactStatus.ACTIVE, mode_name="flirt"
    )
    ignored = CardActionResult(outcome=CardOutcome.APPLIED, status=ContactStatus.IGNORED)
    pending = CardActionResult(outcome=CardOutcome.REFUSED, status=ContactStatus.PENDING)

    assert decision_text(active) == messages.DECISION_REPLY.format(mode="flirt")
    assert decision_text(ignored) == messages.DECISION_IGNORE
    assert decision_text(pending) is None


def test_appending_a_decision_twice_keeps_one_decision() -> None:
    first = CardActionResult(outcome=CardOutcome.APPLIED, status=ContactStatus.IGNORED)
    second = CardActionResult(
        outcome=CardOutcome.ALREADY_APPLIED, status=ContactStatus.ACTIVE, mode_name="flirt"
    )

    once = append_decision("card text", first)
    twice = append_decision(once, second)

    assert twice.count(messages.DECISION_MARKER) == 1
    assert twice.startswith("card text")
    assert twice.endswith(messages.DECISION_REPLY.format(mode="flirt"))


def test_every_outcome_has_an_alert_or_none() -> None:
    assert alert_text(CardOutcome.APPLIED) is None
    assert alert_text(CardOutcome.ALREADY_APPLIED) is None
    assert alert_text(CardOutcome.REFUSED) == messages.ALERT_REFUSED
    assert alert_text(CardOutcome.NOT_FOUND) == messages.ALERT_NOT_FOUND
    assert alert_text(CardOutcome.MODE_UNAVAILABLE) == messages.ALERT_MODE_UNAVAILABLE
