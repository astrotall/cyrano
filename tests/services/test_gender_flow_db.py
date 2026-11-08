from __future__ import annotations

import asyncio

from aiogram.methods import AnswerCallbackQuery, EditMessageText

from cyrano.bot import messages as bot_messages
from cyrano.bot.app import build_dispatcher
from cyrano.bot.callbacks import CardAction, CardCallback, GenderAction, GenderCallback
from cyrano.bot.rendering import card_keyboard, render_card
from cyrano.db.repositories import SqlContactRepository, SqlMessageRepository, SqlModeRepository
from cyrano.domain.enums import ContactGender, ContactStatus, GenderSource
from cyrano.domain.gender import UNRESOLVED_GENDER
from cyrano.domain.models import ContactCreate, ContactReadyEvent
from cyrano.services.card_actions import CardActions
from cyrano.services.card_announcer import CardAnnouncer
from cyrano.services.contact_events import InMemoryContactEvents
from cyrano.services.gender import GenderResolver
from cyrano.services.gender_actions import GenderActions
from cyrano.services.pause_actions import PauseActions
from tests.bot.fakes import OWNER_ID, callback_update, recording_bot
from tests.services.fakes import FakeCardSender

WAIT_ATTEMPTS = 300
WAIT_INTERVAL_SECONDS = 0.01
UNREADABLE_NAME = "Qwxz"


async def test_unknown_gender_hides_reply_then_the_button_shows_it_and_reply_activates(
    contacts: SqlContactRepository, messages: SqlMessageRepository, modes: SqlModeRepository
) -> None:
    default = await modes.get_by_name("default")
    assert default is not None
    created = await contacts.create(ContactCreate(tg_id=1, display_name=UNREADABLE_NAME))
    await contacts.set_status(created.id, ContactStatus.PENDING)
    resolver = GenderResolver()
    bot, session = recording_bot()
    dispatcher = build_dispatcher(
        OWNER_ID,
        CardActions(contacts, modes, resolver),
        PauseActions(contacts),
        GenderActions(contacts, messages, modes, resolver),
    )

    async def press(data: str, update_id: int) -> None:
        await dispatcher.feed_raw_update(bot, callback_update(OWNER_ID, data, update_id=update_id))

    reply = CardCallback(action=CardAction.REPLY, contact_id=created.id, mode_id=default.id)
    await press(reply.pack(), 1)
    refused = await contacts.get_by_id(created.id)
    assert refused is not None
    assert refused.status is ContactStatus.PENDING

    await press(GenderCallback(action=GenderAction.FEMALE, contact_id=created.id).pack(), 2)
    [edit] = session.calls_of(EditMessageText)
    assert edit.text is not None
    assert "Пол: девушка (выбран вами)" in edit.text
    assert edit.reply_markup is not None
    assert any(
        button.text == bot_messages.REPLY_BUTTON.format(mode="default")
        for row in edit.reply_markup.inline_keyboard
        for button in row
    )

    await press(reply.pack(), 3)

    activated = await contacts.get_by_id(created.id)
    assert activated is not None
    assert (activated.status, activated.mode_id, activated.gender) == (
        ContactStatus.ACTIVE,
        default.id,
        ContactGender.FEMALE,
    )
    answers = session.calls_of(AnswerCallbackQuery)
    assert [answer.text for answer in answers] == [bot_messages.ALERT_GENDER_REQUIRED, None, None]


async def test_the_announced_card_hides_reply_for_an_unreadable_name_only(
    contacts: SqlContactRepository, messages: SqlMessageRepository, modes: SqlModeRepository
) -> None:
    unknown = await contacts.create(ContactCreate(tg_id=1, display_name=UNREADABLE_NAME))
    known = await contacts.create(ContactCreate(tg_id=2, display_name="Анна"))
    sender = FakeCardSender()
    ready = InMemoryContactEvents[ContactReadyEvent]()
    announcer = CardAnnouncer(contacts, messages, modes, sender, ready, GenderResolver())
    task = asyncio.create_task(announcer.run())
    try:
        for contact in (unknown, known):
            await ready.publish(ContactReadyEvent(contact_id=contact.id))
        for _ in range(WAIT_ATTEMPTS):
            if len(sender.cards) == 2:
                break
            await asyncio.sleep(WAIT_INTERVAL_SECONDS)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    cards = {card.contact_id: card for card in sender.cards}
    assert cards[unknown.id].gender == UNRESOLVED_GENDER
    assert cards[known.id].gender.source is GenderSource.NAME
    assert cards[known.id].gender.value is ContactGender.FEMALE
    unknown_rows = card_keyboard(cards[unknown.id]).inline_keyboard
    known_rows = card_keyboard(cards[known.id]).inline_keyboard
    assert [b.text for row in unknown_rows[1:] for b in row] == [bot_messages.IGNORE_BUTTON]
    assert len(known_rows) > len(unknown_rows)
    assert "Пол: не определён" in render_card(cards[unknown.id])
