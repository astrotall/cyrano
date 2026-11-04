from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from cyrano.db.repositories import SqlContactRepository, SqlModeRepository, SqlOutboxRepository
from cyrano.domain.enums import (
    ContactStatus,
    LimitScope,
    LimitWindow,
    MessageDirection,
    OutboxSource,
)
from cyrano.domain.models import Contact, Message
from cyrano.domain.outbox import LIMIT_WINDOW_SECONDS, SendLimit
from cyrano.services.repositories import MessageRepository
from cyrano.services.send_limiter import SendLimiter, decide, limits_from, slot_frees_at
from tests.privacy import FAKE_TELEGRAM_ID
from tests.services.outbox_fakes import FAKE_NOW, FakeLimits, stored_contact
from tests.userbot.fakes import InMemoryMessages

CONTACT = 7
OTHER_CONTACT = 8
MINUTE_LIMIT = SendLimit(scope=LimitScope.GLOBAL, window=LimitWindow.MINUTE, maximum=2)
HOUR_LIMIT = SendLimit(scope=LimitScope.GLOBAL, window=LimitWindow.HOUR, maximum=1)


def _ago(seconds: int) -> datetime:
    return FAKE_NOW - timedelta(seconds=seconds)


def _messages(*stored: tuple[int, int]) -> InMemoryMessages:
    fake = InMemoryMessages()
    for number, (contact_id, seconds_ago) in enumerate(stored, start=1):
        fake.stored.append(
            Message(
                id=number,
                contact_id=contact_id,
                direction=MessageDirection.OUTGOING,
                text="fake",
                tg_message_id=number,
                created_at=_ago(seconds_ago),
            )
        )
    return fake


def test_no_history_never_reaches_a_limit() -> None:
    decision = decide([(MINUTE_LIMIT, []), (HOUR_LIMIT, [])], FAKE_NOW)

    assert decision.free_at is None
    assert decision.reached == ()


def test_fewer_timestamps_than_the_limit_is_free() -> None:
    assert slot_frees_at(MINUTE_LIMIT, [_ago(1)], FAKE_NOW) is None


def test_the_slot_frees_when_the_limit_th_most_recent_leaves_the_window() -> None:
    frees_at = slot_frees_at(MINUTE_LIMIT, [_ago(5), _ago(20)], FAKE_NOW)

    assert frees_at == _ago(20) + timedelta(seconds=60)


def test_a_time_equal_to_the_boundary_counts_as_free() -> None:
    assert slot_frees_at(MINUTE_LIMIT, [_ago(5), _ago(60)], FAKE_NOW) is None


def test_one_second_inside_the_boundary_is_not_free() -> None:
    assert slot_frees_at(MINUTE_LIMIT, [_ago(5), _ago(59)], FAKE_NOW) == FAKE_NOW + timedelta(
        seconds=1
    )


def test_several_reached_limits_give_the_latest_time_and_list_each_limit() -> None:
    decision = decide(
        [
            (MINUTE_LIMIT, [_ago(1), _ago(2)]),
            (HOUR_LIMIT, [_ago(100)]),
        ],
        FAKE_NOW,
    )

    assert decision.free_at == _ago(100) + timedelta(hours=1)
    assert decision.reached == (MINUTE_LIMIT, HOUR_LIMIT)


def test_the_maximum_wins_whatever_the_order_of_the_limits() -> None:
    decision = decide(
        [
            (HOUR_LIMIT, [_ago(3590)]),
            (MINUTE_LIMIT, [_ago(1), _ago(2)]),
        ],
        FAKE_NOW,
    )

    assert decision.free_at == _ago(2) + timedelta(seconds=60)


def test_a_limit_that_is_free_does_not_appear_in_the_reached_list() -> None:
    decision = decide([(MINUTE_LIMIT, [_ago(1)]), (HOUR_LIMIT, [_ago(10)])], FAKE_NOW)

    assert decision.reached == (HOUR_LIMIT,)


def test_the_settings_give_all_six_limits() -> None:
    limits = limits_from(FakeLimits(1, 2, 3, 4, 5, 6))

    assert [(limit.scope, limit.window, limit.maximum) for limit in limits] == [
        (LimitScope.GLOBAL, LimitWindow.MINUTE, 1),
        (LimitScope.GLOBAL, LimitWindow.HOUR, 2),
        (LimitScope.GLOBAL, LimitWindow.DAY, 3),
        (LimitScope.CONTACT, LimitWindow.MINUTE, 4),
        (LimitScope.CONTACT, LimitWindow.HOUR, 5),
        (LimitScope.CONTACT, LimitWindow.DAY, 6),
    ]


SIX_LIMITS = [
    (LimitScope.GLOBAL, LimitWindow.MINUTE, FakeLimits(send_limit_minute=3)),
    (LimitScope.GLOBAL, LimitWindow.HOUR, FakeLimits(send_limit_hour=3)),
    (LimitScope.GLOBAL, LimitWindow.DAY, FakeLimits(send_limit_day=3)),
    (LimitScope.CONTACT, LimitWindow.MINUTE, FakeLimits(send_limit_contact_minute=3)),
    (LimitScope.CONTACT, LimitWindow.HOUR, FakeLimits(send_limit_contact_hour=3)),
    (LimitScope.CONTACT, LimitWindow.DAY, FakeLimits(send_limit_contact_day=3)),
]


@pytest.mark.parametrize(("scope", "window", "limits"), SIX_LIMITS)
async def test_each_of_the_six_limits_blocks_exactly_at_its_threshold(
    scope: LimitScope, window: LimitWindow, limits: FakeLimits
) -> None:
    seconds = LIMIT_WINDOW_SECONDS[window]
    below = _messages((CONTACT, 1), (CONTACT, 2))
    at = _messages((CONTACT, 1), (CONTACT, 2), (CONTACT, seconds - 10))

    allowed = await SendLimiter(below, limits).check(CONTACT, FAKE_NOW)
    blocked = await SendLimiter(at, limits).check(CONTACT, FAKE_NOW)

    assert allowed.free_at is None
    assert blocked.free_at == _ago(seconds - 10) + timedelta(seconds=seconds)
    assert [(limit.scope, limit.window) for limit in blocked.reached] == [(scope, window)]


@pytest.mark.parametrize(("scope", "window", "limits"), SIX_LIMITS)
async def test_each_limit_ignores_a_message_exactly_one_window_old(
    scope: LimitScope, window: LimitWindow, limits: FakeLimits
) -> None:
    seconds = LIMIT_WINDOW_SECONDS[window]
    stored = _messages((CONTACT, 1), (CONTACT, 2), (CONTACT, seconds))

    decision = await SendLimiter(stored, limits).check(CONTACT, FAKE_NOW)

    assert decision.free_at is None


@pytest.mark.parametrize(
    "limits",
    [
        FakeLimits(send_limit_contact_minute=2),
        FakeLimits(send_limit_contact_hour=2),
        FakeLimits(send_limit_contact_day=2),
    ],
)
async def test_per_contact_counting_does_not_mix_contacts(limits: FakeLimits) -> None:
    stored = _messages((OTHER_CONTACT, 1), (OTHER_CONTACT, 2), (CONTACT, 3))

    assert (await SendLimiter(stored, limits).check(CONTACT, FAKE_NOW)).free_at is None
    assert (await SendLimiter(stored, limits).check(OTHER_CONTACT, FAKE_NOW)).free_at is not None


async def test_global_counting_adds_all_contacts_together() -> None:
    stored = _messages((OTHER_CONTACT, 1), (CONTACT, 2))

    decision = await SendLimiter(stored, FakeLimits(send_limit_minute=2)).check(CONTACT, FAKE_NOW)

    assert decision.free_at == _ago(2) + timedelta(seconds=60)


async def test_several_limits_at_once_give_the_maximum_through_the_service() -> None:
    stored = _messages((CONTACT, 1), (CONTACT, 3000))
    limits = FakeLimits(send_limit_contact_minute=1, send_limit_hour=2)

    decision = await SendLimiter(stored, limits).check(CONTACT, FAKE_NOW)

    assert decision.free_at == _ago(3000) + timedelta(hours=1)
    assert {(limit.scope, limit.window) for limit in decision.reached} == {
        (LimitScope.CONTACT, LimitWindow.MINUTE),
        (LimitScope.GLOBAL, LimitWindow.HOUR),
    }


async def test_incoming_messages_do_not_count() -> None:
    stored = _messages()
    stored.stored.append(
        Message(
            id=1,
            contact_id=CONTACT,
            direction=MessageDirection.INCOMING,
            text="fake",
            tg_message_id=1,
            created_at=_ago(1),
        )
    )

    decision = await SendLimiter(stored, FakeLimits(send_limit_minute=1)).check(CONTACT, FAKE_NOW)

    assert decision.free_at is None


async def test_usage_reports_the_six_counters_and_the_busiest_contact() -> None:
    stored = _messages((CONTACT, 1), (OTHER_CONTACT, 2), (OTHER_CONTACT, 100))
    limiter = SendLimiter(stored, FakeLimits(10, 20, 30, 3, 6, 9))

    usage = await limiter.usage(FAKE_NOW)

    assert [(item.used, item.limit.maximum, item.contact_id) for item in usage] == [
        (2, 10, None),
        (3, 20, None),
        (3, 30, None),
        (1, 3, CONTACT),
        (2, 6, OTHER_CONTACT),
        (2, 9, OTHER_CONTACT),
    ]


async def test_limits_are_counted_from_real_stored_rows(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    messages: MessageRepository,
) -> None:
    contact: Contact = await stored_contact(
        contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID
    )
    for number, seconds in enumerate((5, 20), start=1):
        await outbox.enqueue(contact.id, "fake", FAKE_NOW, OutboxSource.MANUAL_TEST)
        claimed = await outbox.claim_due(FAKE_NOW)
        assert claimed is not None
        await outbox.complete_send(claimed.id, contact.id, "fake", number, _ago(seconds))

    decision = await SendLimiter(messages, FakeLimits(send_limit_contact_minute=2)).check(
        contact.id, FAKE_NOW
    )

    assert decision.free_at == _ago(20) + timedelta(seconds=60)
