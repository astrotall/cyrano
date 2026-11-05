from __future__ import annotations

import pytest

from cyrano.userbot.filters import (
    TELEGRAM_SERVICE_ACCOUNT_ID,
    Rejection,
    event_rejection,
    outgoing_event_rejection,
    sender_rejection,
)
from tests.userbot.fakes import FakeEvent, FakeSender


def test_private_message_from_a_real_user_is_accepted() -> None:
    assert event_rejection(FakeEvent()) is None
    assert sender_rejection(FakeSender()) is None


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (FakeEvent(out=True), Rejection.OUTGOING),
        (FakeEvent(is_group=True, is_private=False), Rejection.GROUP),
        (FakeEvent(is_channel=True, is_private=False), Rejection.CHANNEL),
        (FakeEvent(is_private=False), Rejection.NOT_PRIVATE),
        (FakeEvent(is_private=None), Rejection.NOT_PRIVATE),
        (FakeEvent(sender_id=TELEGRAM_SERVICE_ACCOUNT_ID), Rejection.SERVICE_ACCOUNT),
    ],
)
def test_event_kinds_that_are_not_private_incoming_messages_are_rejected(
    event: FakeEvent, reason: Rejection
) -> None:
    assert event_rejection(event) is reason


@pytest.mark.parametrize(
    ("sender", "reason"),
    [
        (FakeSender(bot=True), Rejection.BOT),
        (FakeSender(deleted=True), Rejection.DELETED),
        (FakeSender(id=TELEGRAM_SERVICE_ACCOUNT_ID), Rejection.SERVICE_ACCOUNT),
        (FakeSender(is_self=True), Rejection.SELF),
    ],
)
def test_senders_that_are_not_real_users_are_rejected(
    sender: FakeSender, reason: Rejection
) -> None:
    assert sender_rejection(sender) is reason


def test_unset_flags_count_as_a_real_user() -> None:
    assert sender_rejection(FakeSender(bot=None, deleted=None, is_self=None)) is None


OWN_ID = 424242424242


def test_an_outgoing_private_message_to_a_user_is_accepted() -> None:
    assert outgoing_event_rejection(FakeEvent(out=True, sender_id=OWN_ID)) is None


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (FakeEvent(out=False), Rejection.NOT_OUTGOING),
        (FakeEvent(out=True, is_group=True, is_private=False), Rejection.GROUP),
        (FakeEvent(out=True, is_channel=True, is_private=False), Rejection.CHANNEL),
        (FakeEvent(out=True, is_private=False), Rejection.NOT_PRIVATE),
        (FakeEvent(out=True, is_private=None), Rejection.NOT_PRIVATE),
        (FakeEvent(out=True, chat_id=None), Rejection.NO_PEER),
        (FakeEvent(out=True, chat_id=0), Rejection.NO_PEER),
        (FakeEvent(out=True, chat_id=-1001234567890), Rejection.NO_PEER),
        (FakeEvent(out=True, chat_id=TELEGRAM_SERVICE_ACCOUNT_ID), Rejection.SERVICE_ACCOUNT),
    ],
)
def test_outgoing_events_that_are_not_private_user_chats_are_rejected(
    event: FakeEvent, reason: Rejection
) -> None:
    assert outgoing_event_rejection(event) is reason


def test_the_peer_of_an_outgoing_message_goes_through_the_same_checks_as_a_sender() -> None:
    assert sender_rejection(FakeSender(bot=True)) is Rejection.BOT
    assert sender_rejection(FakeSender(deleted=True)) is Rejection.DELETED
    assert sender_rejection(FakeSender(is_self=True)) is Rejection.SELF
