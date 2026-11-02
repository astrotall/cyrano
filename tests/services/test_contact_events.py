from __future__ import annotations

import asyncio
import logging

import pytest

from cyrano.domain.models import NewContactEvent
from cyrano.services.contact_events import (
    EVENT_QUEUE_SIZE,
    ContactEventPublisher,
    InMemoryContactEvents,
)


def _event(number: int) -> NewContactEvent:
    return NewContactEvent(contact_id=number, message_id=number * 10)


async def _next(events: InMemoryContactEvents) -> NewContactEvent:
    return await anext(events.subscribe())


def test_queue_size_is_one_thousand() -> None:
    assert EVENT_QUEUE_SIZE == 1000


async def test_events_reach_the_subscriber_in_order() -> None:
    events = InMemoryContactEvents()
    publisher: ContactEventPublisher = events
    for number in (1, 2, 3):
        await publisher.publish(_event(number))

    subscription = events.subscribe()
    received = [await anext(subscription) for _ in range(3)]

    assert received == [_event(1), _event(2), _event(3)]
    assert events.published == 3
    assert events.dropped == 0


async def test_subscriber_waits_for_the_next_event() -> None:
    events = InMemoryContactEvents()
    waiting = asyncio.create_task(_next(events))
    await asyncio.sleep(0)
    assert not waiting.done()

    await events.publish(_event(7))

    assert await asyncio.wait_for(waiting, timeout=1) == _event(7)


async def test_full_queue_drops_the_new_event_and_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events = InMemoryContactEvents(max_size=2)
    await events.publish(_event(1))
    await events.publish(_event(2))

    with caplog.at_level(logging.WARNING):
        await events.publish(_event(3))
        await events.publish(_event(4))

    assert events.published == 2
    assert events.dropped == 2
    subscription = events.subscribe()
    assert [await anext(subscription), await anext(subscription)] == [_event(1), _event(2)]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert warnings[-1].__dict__["dropped"] == 2
    assert warnings[-1].__dict__["queued"] == 2


async def test_drop_warning_carries_counters_only(caplog: pytest.LogCaptureFixture) -> None:
    events = InMemoryContactEvents(max_size=1)
    await events.publish(NewContactEvent(contact_id=424242, message_id=737373))

    with caplog.at_level(logging.WARNING):
        await events.publish(NewContactEvent(contact_id=909090, message_id=818181))

    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "909090" not in text
    assert "818181" not in text
