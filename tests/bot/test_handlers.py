from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
)

from cyrano.bot import messages
from cyrano.bot.app import build_dispatcher
from cyrano.bot.callbacks import CardAction, CardCallback
from cyrano.domain.enums import ContactStatus
from cyrano.domain.models import Contact, ModeCreate
from cyrano.services.card_actions import CardActions
from tests.bot.fakes import (
    OWNER_ID,
    STRANGER_ID,
    RecordingSession,
    callback_update,
    message_update,
    recording_bot,
)
from tests.privacy import assert_absent
from tests.services.fakes import InMemoryModes
from tests.userbot.fakes import InMemoryContacts

CARD_TEXT = "Новый контакт\n\nИмя: Fakefirst"
STRANGER_TEXT = "stranger-text-that-must-not-leak"
CONTACT_TG_ID = 5001


@dataclass
class Rig:
    dispatcher: Dispatcher
    bot: Bot
    session: RecordingSession
    contacts: InMemoryContacts
    modes: InMemoryModes
    contact: Contact

    async def feed(self, update: dict[str, object]) -> None:
        await self.dispatcher.feed_raw_update(self.bot, update)

    async def press(self, callback: CardCallback, **kwargs: object) -> None:
        await self.feed(callback_update(OWNER_ID, callback.pack(), **kwargs))  # type: ignore[arg-type]

    async def current(self) -> Contact:
        contact = await self.contacts.get_by_id(self.contact.id)
        assert contact is not None
        return contact


async def _rig(
    *,
    status: ContactStatus = ContactStatus.PENDING,
    mode_id: int | None = None,
    session: RecordingSession | None = None,
    contacts: InMemoryContacts | None = None,
) -> Rig:
    contacts = contacts if contacts is not None else InMemoryContacts()
    modes = InMemoryModes()
    await modes.create(ModeCreate(name="default", prompt_fragment=""))
    await modes.create(ModeCreate(name="flirt", prompt_fragment=""))
    await modes.create(ModeCreate(name="retired", prompt_fragment="", enabled=False))
    contact = await contacts.seed(CONTACT_TG_ID, status=status, mode_id=mode_id)
    bot, session = recording_bot(session)
    dispatcher = build_dispatcher(OWNER_ID, CardActions(contacts, modes))
    return Rig(dispatcher, bot, session, contacts, modes, contact)


def _edited(edit: EditMessageText) -> str:
    assert edit.text is not None
    return edit.text


def _reply(rig: Rig, mode_id: int) -> CardCallback:
    return CardCallback(action=CardAction.REPLY, contact_id=rig.contact.id, mode_id=mode_id)


def _ignore(rig: Rig) -> CardCallback:
    return CardCallback(action=CardAction.IGNORE, contact_id=rig.contact.id)


def _assert_only_the_owner_is_addressed(session: RecordingSession) -> None:
    addressed = {chat_id for chat_id in session.chat_ids() if chat_id is not None}
    assert addressed <= {OWNER_ID}


async def test_start_from_the_owner_gets_the_greeting() -> None:
    rig = await _rig()

    await rig.feed(message_update(OWNER_ID, "/start"))

    [sent] = rig.session.calls_of(SendMessage)
    assert sent.chat_id == OWNER_ID
    assert sent.text == messages.START_TEXT
    _assert_only_the_owner_is_addressed(rig.session)


async def test_start_from_the_owner_in_a_group_gets_no_answer() -> None:
    rig = await _rig()

    await rig.feed(message_update(OWNER_ID, "/start", chat_id=-100500, chat_type="group"))

    assert rig.session.calls == []


async def test_text_from_the_owner_without_a_handler_gets_no_answer() -> None:
    rig = await _rig()

    await rig.feed(message_update(OWNER_ID, "hello"))

    assert rig.session.calls == []


async def test_a_message_from_a_stranger_gets_no_reply_and_changes_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = await _rig()

    with caplog.at_level(logging.DEBUG):
        await rig.feed(message_update(STRANGER_ID, "/start"))
        await rig.feed(message_update(STRANGER_ID, STRANGER_TEXT, update_id=3))

    assert rig.session.calls == []
    assert (await rig.current()).status is ContactStatus.PENDING
    assert rig.contacts.status_changes == []
    logged = "".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert_absent(STRANGER_ID, logged)
    assert STRANGER_TEXT not in logged


async def test_a_message_without_a_sender_is_dropped() -> None:
    rig = await _rig()
    update = message_update(OWNER_ID, "/start")
    message = update["message"]
    assert isinstance(message, dict)
    del message["from"]

    await rig.feed(update)

    assert rig.session.calls == []


async def test_a_button_press_from_a_stranger_changes_nothing_and_is_not_answered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = await _rig()

    with caplog.at_level(logging.DEBUG):
        await rig.feed(callback_update(STRANGER_ID, _reply(rig, 1).pack(), chat_id=STRANGER_ID))
        await rig.feed(callback_update(STRANGER_ID, _ignore(rig).pack(), update_id=3))

    assert rig.session.calls == []
    assert (await rig.current()).status is ContactStatus.PENDING
    assert rig.contacts.status_changes == []
    logged = "".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert_absent(STRANGER_ID, logged)


async def test_reply_activates_the_contact_with_the_mode_and_updates_the_card() -> None:
    rig = await _rig()

    await rig.press(_reply(rig, 2), card_text=CARD_TEXT)

    current = await rig.current()
    assert (current.status, current.mode_id) == (ContactStatus.ACTIVE, 2)
    [edit] = rig.session.calls_of(EditMessageText)
    assert edit.text == f"{CARD_TEXT}{messages.DECISION_MARKER} " + messages.DECISION_REPLY.format(
        mode="flirt"
    )
    assert edit.reply_markup is None
    [answer] = rig.session.calls_of(AnswerCallbackQuery)
    assert answer.text is None
    _assert_only_the_owner_is_addressed(rig.session)


async def test_ignore_ignores_the_contact_and_updates_the_card() -> None:
    rig = await _rig()

    await rig.press(_ignore(rig))

    assert (await rig.current()).status is ContactStatus.IGNORED
    [edit] = rig.session.calls_of(EditMessageText)
    assert _edited(edit).endswith(messages.DECISION_IGNORE)
    assert edit.reply_markup is None
    assert len(rig.session.calls_of(AnswerCallbackQuery)) == 1


async def test_a_double_click_shows_one_decision_and_changes_nothing_twice() -> None:
    rig = await _rig()

    await rig.press(_reply(rig, 1))
    first_edit = rig.session.calls_of(EditMessageText)[0]
    await rig.press(_reply(rig, 1), card_text=first_edit.text, update_id=3)

    assert rig.contacts.status_changes == [(rig.contact.id, ContactStatus.ACTIVE)]
    second_edit = rig.session.calls_of(EditMessageText)[1]
    assert second_edit.text == first_edit.text
    assert _edited(second_edit).count(messages.DECISION_MARKER) == 1
    assert len(rig.session.calls_of(AnswerCallbackQuery)) == 2


async def test_a_stale_card_shows_the_current_state_of_the_contact() -> None:
    rig = await _rig(status=ContactStatus.ACTIVE, mode_id=1)

    await rig.press(_reply(rig, 1))

    [edit] = rig.session.calls_of(EditMessageText)
    assert _edited(edit).endswith(messages.DECISION_REPLY.format(mode="default"))
    assert (await rig.current()).mode_id == 1


async def test_ignoring_an_already_ignored_contact_shows_ignored() -> None:
    rig = await _rig(status=ContactStatus.IGNORED)

    await rig.press(_ignore(rig))

    [edit] = rig.session.calls_of(EditMessageText)
    assert _edited(edit).endswith(messages.DECISION_IGNORE)


async def test_reply_after_ignore_is_refused_with_an_alert_and_the_keyboard_is_removed() -> None:
    rig = await _rig()
    await rig.press(_ignore(rig))

    await rig.press(_reply(rig, 1), update_id=3)

    assert (await rig.current()).status is ContactStatus.IGNORED
    [answer] = rig.session.calls_of(AnswerCallbackQuery)[1:]
    assert (answer.text, answer.show_alert) == (messages.ALERT_REFUSED, True)
    [removal] = rig.session.calls_of(EditMessageReplyMarkup)
    assert removal.reply_markup is None


async def test_ignore_on_an_active_contact_is_refused_and_changes_nothing() -> None:
    rig = await _rig(status=ContactStatus.ACTIVE, mode_id=1)

    await rig.press(_ignore(rig))

    current = await rig.current()
    assert (current.status, current.mode_id) == (ContactStatus.ACTIVE, 1)
    assert rig.contacts.status_changes == []
    [answer] = rig.session.calls_of(AnswerCallbackQuery)
    assert answer.text == messages.ALERT_REFUSED
    assert len(rig.session.calls_of(EditMessageReplyMarkup)) == 1
    assert rig.session.calls_of(EditMessageText) == []


async def test_a_stale_reply_with_another_mode_does_not_re_mode_an_active_contact() -> None:
    rig = await _rig(status=ContactStatus.ACTIVE, mode_id=1)

    await rig.press(_reply(rig, 2))

    assert (await rig.current()).mode_id == 1
    assert rig.session.calls_of(AnswerCallbackQuery)[0].text == messages.ALERT_REFUSED


async def test_an_unknown_contact_gets_an_alert_and_the_keyboard_is_removed() -> None:
    rig = await _rig()
    unknown = CardCallback(action=CardAction.IGNORE, contact_id=9999)

    await rig.press(unknown)

    [answer] = rig.session.calls_of(AnswerCallbackQuery)
    assert answer.text == messages.ALERT_NOT_FOUND
    assert len(rig.session.calls_of(EditMessageReplyMarkup)) == 1


async def test_a_disabled_mode_gets_an_alert_and_keeps_the_card_pending_with_buttons() -> None:
    rig = await _rig()

    await rig.press(_reply(rig, 3))

    assert (await rig.current()).status is ContactStatus.PENDING
    assert rig.contacts.status_changes == []
    [answer] = rig.session.calls_of(AnswerCallbackQuery)
    assert answer.text == messages.ALERT_MODE_UNAVAILABLE
    assert rig.session.calls_of(EditMessageText) == []
    assert rig.session.calls_of(EditMessageReplyMarkup) == []


async def test_a_missing_mode_gets_an_alert() -> None:
    rig = await _rig()

    await rig.press(_reply(rig, 4242))

    assert rig.session.calls_of(AnswerCallbackQuery)[0].text == messages.ALERT_MODE_UNAVAILABLE
    assert (await rig.current()).status is ContactStatus.PENDING


async def test_a_card_that_cannot_be_edited_is_only_answered() -> None:
    session = RecordingSession(
        {
            EditMessageText: [
                TelegramBadRequest(
                    EditMessageText(text="x"), "Bad Request: message can't be edited"
                )
            ]
        }
    )
    rig = await _rig(session=session)

    await rig.press(_ignore(rig))

    assert (await rig.current()).status is ContactStatus.IGNORED
    assert len(rig.session.calls_of(AnswerCallbackQuery)) == 1


async def test_an_inaccessible_card_message_is_only_answered() -> None:
    rig = await _rig()

    await rig.press(_ignore(rig), inaccessible=True)

    assert (await rig.current()).status is ContactStatus.IGNORED
    assert rig.session.calls_of(EditMessageText) == []
    assert len(rig.session.calls_of(AnswerCallbackQuery)) == 1


async def test_a_card_message_without_text_is_only_answered() -> None:
    rig = await _rig()

    await rig.press(_ignore(rig), card_text=None)

    assert rig.session.calls_of(EditMessageText) == []
    assert len(rig.session.calls_of(AnswerCallbackQuery)) == 1


async def test_a_failing_action_still_answers_the_callback_with_an_alert() -> None:
    class _BrokenContacts(InMemoryContacts):
        async def get_by_id(self, contact_id: int) -> Contact | None:
            raise ConnectionError("fake database failure")

    rig = await _rig(contacts=_BrokenContacts())

    await rig.press(_ignore(rig))

    [answer] = rig.session.calls_of(AnswerCallbackQuery)
    assert answer.text == messages.ALERT_FAILED
    assert rig.session.calls_of(EditMessageText) == []


async def test_an_expired_callback_query_does_not_break_the_handler() -> None:
    session = RecordingSession(
        {
            AnswerCallbackQuery: [
                TelegramBadRequest(
                    AnswerCallbackQuery(callback_query_id="x"), "Bad Request: query is too old"
                )
            ]
        }
    )
    rig = await _rig(session=session)

    await rig.press(_ignore(rig))

    assert (await rig.current()).status is ContactStatus.IGNORED


async def test_the_decision_keeps_html_in_the_card_escaped() -> None:
    rig = await _rig()

    await rig.press(_ignore(rig), card_text="a <b>x</b> & c")

    [edit] = rig.session.calls_of(EditMessageText)
    assert _edited(edit).startswith("a &lt;b&gt;x&lt;/b&gt; &amp; c")


async def test_callbacks_log_internal_ids_and_outcomes_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = await _rig()

    with caplog.at_level(logging.INFO):
        await rig.press(_reply(rig, 1))

    [record] = [r for r in caplog.records if r.getMessage() == "card button handled"]
    assert (record.__dict__["contact_id"], record.__dict__["outcome"]) == (
        rig.contact.id,
        "applied",
    )
    assert_absent(OWNER_ID, "".join(r.getMessage() + str(r.__dict__) for r in caplog.records))
