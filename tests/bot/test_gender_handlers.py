from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText
from aiogram.types import InlineKeyboardMarkup

from cyrano.bot import messages
from cyrano.bot.app import build_dispatcher
from cyrano.bot.callbacks import CardAction, CardCallback, GenderAction, GenderCallback
from cyrano.domain.enums import ContactGender, ContactStatus
from cyrano.domain.models import Contact, ModeCreate
from cyrano.services.card_actions import CardActions
from cyrano.services.gender import GenderResolver
from cyrano.services.gender_actions import GenderActions
from cyrano.services.pause_actions import PauseActions
from tests.bot.fakes import (
    OWNER_ID,
    STRANGER_ID,
    RecordingSession,
    callback_update,
    recording_bot,
)
from tests.privacy import assert_absent
from tests.services.fakes import InMemoryModes
from tests.userbot.fakes import InMemoryContacts, InMemoryMessages

CONTACT_TG_ID = 5001
NAME_MARKER = "Gender-Handler-Name-zq"
FEMALE = ContactGender.FEMALE
MALE = ContactGender.MALE


@dataclass
class Rig:
    dispatcher: Dispatcher
    bot: Bot
    session: RecordingSession
    contacts: InMemoryContacts
    contact: Contact

    async def press_gender(
        self, action: GenderAction, *, contact_id: int | None = None, user_id: int = OWNER_ID
    ) -> None:
        callback = GenderCallback(
            action=action, contact_id=self.contact.id if contact_id is None else contact_id
        )
        await self.dispatcher.feed_raw_update(
            self.bot, callback_update(user_id, callback.pack(), card_text="Новый контакт")
        )

    async def press_card(self, callback: CardCallback) -> None:
        await self.dispatcher.feed_raw_update(
            self.bot, callback_update(OWNER_ID, callback.pack(), update_id=3)
        )

    async def current(self) -> Contact:
        found = await self.contacts.get_by_id(self.contact.id)
        assert found is not None
        return found


async def _rig(
    *,
    status: ContactStatus = ContactStatus.PENDING,
    mode_id: int | None = None,
    name: str | None = NAME_MARKER,
    default: ContactGender | None = None,
    gender: ContactGender | None = None,
    session: RecordingSession | None = None,
) -> Rig:
    contacts = InMemoryContacts()
    modes = InMemoryModes()
    await modes.create(ModeCreate(name="default", prompt_fragment=""))
    await modes.create(ModeCreate(name="flirt", prompt_fragment=""))
    contact = await contacts.seed(
        CONTACT_TG_ID, status=status, mode_id=mode_id, display_name=name, gender=gender
    )
    bot, session = recording_bot(session)
    resolver = GenderResolver(default)
    dispatcher = build_dispatcher(
        OWNER_ID,
        CardActions(contacts, modes, resolver),
        PauseActions(contacts),
        GenderActions(contacts, InMemoryMessages(), modes, resolver),
    )
    return Rig(dispatcher, bot, session, contacts, contact)


def _edited(rig: Rig) -> EditMessageText:
    [edit] = rig.session.calls_of(EditMessageText)
    return edit


def _button_texts(markup: object) -> list[list[str]]:
    assert isinstance(markup, InlineKeyboardMarkup)
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _answer(rig: Rig) -> AnswerCallbackQuery:
    [answer] = rig.session.calls_of(AnswerCallbackQuery)
    return answer


async def test_a_gender_button_stores_the_choice_and_rebuilds_the_card() -> None:
    rig = await _rig()

    await rig.press_gender(GenderAction.FEMALE)

    assert (await rig.current()).gender is FEMALE
    edit = _edited(rig)
    assert edit.text is not None
    assert "Пол: девушка (выбран вами)" in edit.text
    rows = _button_texts(edit.reply_markup)
    assert rows[0] == [f"{messages.GENDER_CHECK_MARK}Девушка", "Парень"]
    assert rows[1:] == [
        [messages.REPLY_BUTTON.format(mode="default")],
        [messages.REPLY_BUTTON.format(mode="flirt")],
        [messages.IGNORE_BUTTON],
    ]
    assert (_answer(rig).text, _answer(rig).show_alert) == (None, False)
    assert edit.chat_id == OWNER_ID or edit.chat_id is not None


async def test_the_male_button_marks_the_other_side() -> None:
    rig = await _rig()

    await rig.press_gender(GenderAction.MALE)

    edit = _edited(rig)
    assert edit.text is not None
    assert "Пол: парень (выбран вами)" in edit.text
    assert _button_texts(edit.reply_markup)[0] == ["Девушка", f"{messages.GENDER_CHECK_MARK}Парень"]
    assert (await rig.current()).gender is MALE


async def test_a_second_choice_replaces_the_first_and_every_press_is_answered() -> None:
    rig = await _rig()

    await rig.press_gender(GenderAction.FEMALE)
    await rig.press_gender(GenderAction.MALE)

    assert (await rig.current()).gender is MALE
    assert len(rig.session.calls_of(EditMessageText)) == 2
    assert len(rig.session.calls_of(AnswerCallbackQuery)) == 2


async def test_the_same_choice_twice_is_idempotent() -> None:
    rig = await _rig()

    await rig.press_gender(GenderAction.FEMALE)
    await rig.press_gender(GenderAction.FEMALE)

    first, second = rig.session.calls_of(EditMessageText)
    assert first.text == second.text
    assert (await rig.current()).gender is FEMALE
    assert [a.text for a in rig.session.calls_of(AnswerCallbackQuery)] == [None, None]


@pytest.mark.parametrize(
    "status", [ContactStatus.ACTIVE, ContactStatus.PAUSED, ContactStatus.IGNORED]
)
async def test_a_stale_card_still_stores_the_choice_and_keeps_only_the_gender_row(
    status: ContactStatus,
) -> None:
    rig = await _rig(status=status, mode_id=1 if status is not ContactStatus.IGNORED else None)

    await rig.press_gender(GenderAction.MALE)

    current = await rig.current()
    assert current.gender is MALE
    assert current.status is status
    edit = _edited(rig)
    assert edit.text is not None
    assert f"{messages.LABEL_STATUS}: {messages.STATUS_NAMES[status]}" in edit.text
    assert len(_button_texts(edit.reply_markup)) == 1
    assert rig.contacts.status_changes == []


async def test_an_unknown_contact_gets_an_alert_and_no_card_edit() -> None:
    rig = await _rig()

    await rig.press_gender(GenderAction.FEMALE, contact_id=9999)

    assert rig.session.calls_of(EditMessageText) == []
    answer = _answer(rig)
    assert (answer.text, answer.show_alert) == (messages.ALERT_NOT_FOUND, True)
    assert (await rig.current()).gender is None


async def test_an_unchanged_card_is_not_an_error() -> None:
    session = RecordingSession(
        {
            EditMessageText: [
                TelegramBadRequest(
                    EditMessageText(text="x"), "Bad Request: message is not modified"
                )
            ]
        }
    )
    rig = await _rig(session=session)

    await rig.press_gender(GenderAction.FEMALE)

    assert (await rig.current()).gender is FEMALE
    answer = _answer(rig)
    assert (answer.text, answer.show_alert) == (None, False)


async def test_a_card_that_cannot_be_edited_is_only_answered() -> None:
    session = RecordingSession(
        {
            EditMessageText: [
                TelegramBadRequest(EditMessageText(text="x"), "Bad Request: can't be edited")
            ]
        }
    )
    rig = await _rig(session=session)

    await rig.press_gender(GenderAction.MALE)

    assert (await rig.current()).gender is MALE
    assert _answer(rig).text is None


async def test_an_inaccessible_card_message_is_only_answered() -> None:
    rig = await _rig()
    callback = GenderCallback(action=GenderAction.FEMALE, contact_id=rig.contact.id)

    await rig.dispatcher.feed_raw_update(
        rig.bot, callback_update(OWNER_ID, callback.pack(), inaccessible=True)
    )

    assert (await rig.current()).gender is FEMALE
    assert rig.session.calls_of(EditMessageText) == []
    assert len(rig.session.calls_of(AnswerCallbackQuery)) == 1


async def test_a_message_without_text_is_not_edited() -> None:
    rig = await _rig()
    callback = GenderCallback(action=GenderAction.FEMALE, contact_id=rig.contact.id)

    await rig.dispatcher.feed_raw_update(
        rig.bot, callback_update(OWNER_ID, callback.pack(), card_text=None)
    )

    assert (await rig.current()).gender is FEMALE
    assert rig.session.calls_of(EditMessageText) == []
    assert len(rig.session.calls_of(AnswerCallbackQuery)) == 1


async def test_a_press_from_a_stranger_changes_nothing_and_is_not_answered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = await _rig()

    with caplog.at_level(logging.DEBUG):
        await rig.press_gender(GenderAction.FEMALE, user_id=STRANGER_ID)

    assert rig.session.calls == []
    assert (await rig.current()).gender is None
    logged = "".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert_absent(STRANGER_ID, logged)


async def test_a_failing_storage_gives_the_failure_alert_and_is_still_answered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = await _rig()

    async def broken(contact_id: int, gender: ContactGender | None) -> Contact:
        raise ConnectionError(NAME_MARKER)

    rig.contacts.set_gender = broken  # type: ignore[method-assign]

    with caplog.at_level(logging.DEBUG):
        await rig.press_gender(GenderAction.FEMALE)

    answer = _answer(rig)
    assert (answer.text, answer.show_alert) == (messages.ALERT_FAILED, True)
    assert rig.session.calls_of(EditMessageText) == []
    logged = "".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert_absent(NAME_MARKER, logged)


async def test_reply_with_an_unknown_gender_is_refused_with_a_short_alert() -> None:
    rig = await _rig()

    await rig.press_card(CardCallback(action=CardAction.REPLY, contact_id=1, mode_id=1))

    current = await rig.current()
    assert current.status is ContactStatus.PENDING
    assert rig.contacts.status_changes == []
    answer = _answer(rig)
    assert (answer.text, answer.show_alert) == (messages.ALERT_GENDER_REQUIRED, True)
    assert rig.session.calls_of(EditMessageText) == []
    assert rig.session.calls_of(EditMessageReplyMarkup) == []


async def test_ignore_still_works_for_an_unknown_gender() -> None:
    rig = await _rig()

    await rig.press_card(CardCallback(action=CardAction.IGNORE, contact_id=1))

    assert (await rig.current()).status is ContactStatus.IGNORED


async def test_the_old_card_without_a_gender_row_keeps_working_for_a_known_gender() -> None:
    rig = await _rig(name="Анна")

    await rig.press_card(CardCallback(action=CardAction.REPLY, contact_id=1, mode_id=2))

    current = await rig.current()
    assert (current.status, current.mode_id) == (ContactStatus.ACTIVE, 2)
    assert _answer(rig).text is None


async def test_the_full_flow_unknown_gender_then_choice_then_reply() -> None:
    rig = await _rig()

    await rig.press_card(CardCallback(action=CardAction.REPLY, contact_id=1, mode_id=1))
    await rig.press_gender(GenderAction.FEMALE)
    await rig.press_card(CardCallback(action=CardAction.REPLY, contact_id=1, mode_id=1))

    current = await rig.current()
    assert (current.status, current.mode_id, current.gender) == (
        ContactStatus.ACTIVE,
        1,
        FEMALE,
    )
    answers = rig.session.calls_of(AnswerCallbackQuery)
    assert [a.text for a in answers] == [messages.ALERT_GENDER_REQUIRED, None, None]


async def test_the_logs_carry_no_name_and_no_gender_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = await _rig()

    with caplog.at_level(logging.DEBUG):
        await rig.press_gender(GenderAction.FEMALE)
        await rig.press_gender(GenderAction.MALE)
        await rig.press_gender(GenderAction.MALE, contact_id=9999)

    logged = "".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert NAME_MARKER not in logged
    assert "female" not in logged
    assert "male" not in logged
    assert "gender button handled" in logged
