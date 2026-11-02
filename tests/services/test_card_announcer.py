from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from cyrano.domain.enums import ContactStatus, MessageDirection
from cyrano.domain.models import (
    CARD_MESSAGES_LIMIT,
    Contact,
    ContactProfile,
    ContactReadyEvent,
    MessageCreate,
    ModeCreate,
)
from cyrano.services.card_announcer import (
    CARD_SWEEP_INTERVAL,
    CARD_WITHOUT_PROFILE_DELAY,
    CardAnnouncer,
)
from cyrano.services.contact_events import InMemoryContactEvents
from tests.services.fakes import FakeCardSender, InMemoryModes
from tests.userbot.fakes import InMemoryContacts, InMemoryMessages

NOW = datetime(2025, 6, 7, 8, 9, 10, tzinfo=UTC)
RECENT = NOW - timedelta(seconds=CARD_WITHOUT_PROFILE_DELAY - 1)
OLD = NOW - timedelta(seconds=CARD_WITHOUT_PROFILE_DELAY)
LONG_INTERVAL = 3600.0
SHORT_INTERVAL = 0.02
WAIT_ATTEMPTS = 300
WAIT_INTERVAL_SECONDS = 0.01
COLLECTED = ContactProfile(collected_at=NOW, bio="fake bio").model_dump(mode="json")


@dataclass
class Rig:
    contacts: InMemoryContacts
    messages: InMemoryMessages
    modes: InMemoryModes
    sender: FakeCardSender
    ready: InMemoryContactEvents[ContactReadyEvent]
    announcer: CardAnnouncer


async def _rig(
    *,
    sender: FakeCardSender | None = None,
    contacts: InMemoryContacts | None = None,
    sweep_interval: float = LONG_INTERVAL,
) -> Rig:
    contacts = contacts if contacts is not None else InMemoryContacts()
    messages = InMemoryMessages()
    modes = InMemoryModes()
    await modes.create(ModeCreate(name="default", prompt_fragment=""))
    await modes.create(ModeCreate(name="flirt", prompt_fragment=""))
    await modes.create(ModeCreate(name="retired", prompt_fragment="", enabled=False))
    sender = sender if sender is not None else FakeCardSender()
    ready = InMemoryContactEvents[ContactReadyEvent]()
    announcer = CardAnnouncer(
        contacts,
        messages,
        modes,
        sender,
        ready,
        clock=lambda: NOW,
        sweep_interval=sweep_interval,
    )
    return Rig(contacts, messages, modes, sender, ready, announcer)


@asynccontextmanager
async def _running(rig: Rig) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.create_task(rig.announcer.run())
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _until(condition: Callable[[], Awaitable[bool] | bool]) -> None:
    for _ in range(WAIT_ATTEMPTS):
        outcome = condition()
        if (await outcome) if isinstance(outcome, Awaitable) else outcome:
            return
        await asyncio.sleep(WAIT_INTERVAL_SECONDS)
    pytest.fail("the condition was never met")


async def _status(rig: Rig, contact: Contact) -> ContactStatus:
    current = await rig.contacts.get_by_id(contact.id)
    assert current is not None
    return current.status


async def _settled(rig: Rig) -> None:
    await asyncio.sleep(WAIT_INTERVAL_SECONDS * 10)


async def _seed(rig: Rig, tg_id: int, **kwargs: object) -> Contact:
    return await rig.contacts.seed(tg_id, **kwargs)  # type: ignore[arg-type]


async def test_a_ready_event_sends_the_card_and_then_makes_the_contact_pending() -> None:
    rig = await _rig()
    contact = await _seed(rig, 1, profile=COLLECTED, username="fake_user", display_name="Fake Name")
    for number in range(1, 6):
        await rig.messages.append(
            MessageCreate(
                contact_id=contact.id,
                direction=MessageDirection.INCOMING,
                text=f"text {number}",
                tg_message_id=number,
            )
        )
    async with _running(rig):
        await rig.ready.publish(ContactReadyEvent(contact_id=contact.id))
        await _until(lambda: _is(rig, contact, ContactStatus.PENDING))

    [card] = rig.sender.cards
    assert card.contact_id == contact.id
    assert card.messages == ["text 3", "text 4", "text 5"]
    assert len(card.messages) == CARD_MESSAGES_LIMIT
    assert [m.name for m in card.modes] == ["default", "flirt"]
    assert card.profile_collected is True
    assert rig.contacts.status_changes == [(contact.id, ContactStatus.PENDING)]
    assert rig.announcer.announced == 1


async def _is(rig: Rig, contact: Contact, status: ContactStatus) -> bool:
    return await _status(rig, contact) is status


async def test_the_status_changes_only_after_the_sender_returned() -> None:
    seen: list[ContactStatus] = []

    class _ObservingSender(FakeCardSender):
        async def send_card(self, card: object) -> bool:
            seen.append(await _status(rig, contact))
            return True

    rig = await _rig(sender=_ObservingSender())
    contact = await _seed(rig, 1, profile=COLLECTED)
    async with _running(rig):
        await rig.ready.publish(ContactReadyEvent(contact_id=contact.id))
        await _until(lambda: _is(rig, contact, ContactStatus.PENDING))

    assert seen == [ContactStatus.NEW]


async def test_an_event_for_an_uncollected_recent_contact_still_sends_the_card() -> None:
    rig = await _rig()
    contact = await _seed(rig, 1, created_at=RECENT)
    async with _running(rig):
        await rig.ready.publish(ContactReadyEvent(contact_id=contact.id))
        await _until(lambda: _is(rig, contact, ContactStatus.PENDING))

    assert [c.profile_collected for c in rig.sender.cards] == [False]


async def test_an_undelivered_card_leaves_the_contact_new_and_the_next_sweep_retries() -> None:
    rig = await _rig(sender=FakeCardSender(results=[False, True]), sweep_interval=SHORT_INTERVAL)
    contact = await _seed(rig, 1, profile=COLLECTED)
    async with _running(rig):
        await _until(lambda: _is(rig, contact, ContactStatus.PENDING))

    assert len(rig.sender.cards) == 2
    assert rig.announcer.deferred == 1
    assert rig.contacts.status_changes == [(contact.id, ContactStatus.PENDING)]


async def test_a_card_that_was_not_delivered_stays_new_until_something_retries() -> None:
    rig = await _rig(sender=FakeCardSender(results=[False]))
    contact = await _seed(rig, 1, profile=COLLECTED)
    async with _running(rig):
        await _until(lambda: _deferred(rig))
        await _settled(rig)

    assert await _status(rig, contact) is ContactStatus.NEW
    assert rig.contacts.status_changes == []


async def _deferred(rig: Rig) -> bool:
    return rig.announcer.deferred == 1


async def test_a_sender_exception_leaves_the_contact_new_and_does_not_stop_the_loop() -> None:
    rig = await _rig(sender=FakeCardSender(failing_ids=frozenset({1})))
    broken = await _seed(rig, 11, profile=COLLECTED)
    healthy = await _seed(rig, 12, profile=COLLECTED)
    assert broken.id == 1
    async with _running(rig) as task:
        await _until(lambda: _is(rig, healthy, ContactStatus.PENDING))
        assert not task.done()

    assert await _status(rig, broken) is ContactStatus.NEW
    assert rig.announcer.failed == 1


async def test_a_failed_status_write_after_delivery_leaves_the_contact_new_for_the_next_sweep() -> (
    None
):
    class _FailOnce(InMemoryContacts):
        failures = 1

        async def set_status(
            self, contact_id: int, status: ContactStatus, *, mode_id: int | None = None
        ) -> Contact:
            if self.failures:
                self.failures -= 1
                raise ConnectionError("fake database failure")
            return await super().set_status(contact_id, status, mode_id=mode_id)

    rig = await _rig(contacts=_FailOnce(), sweep_interval=SHORT_INTERVAL)
    contact = await _seed(rig, 1, profile=COLLECTED)
    async with _running(rig):
        await _until(lambda: _is(rig, contact, ContactStatus.PENDING))

    assert len(rig.sender.cards) == 2
    assert rig.announcer.failed == 1
    assert rig.announcer.announced == 1


@pytest.mark.parametrize(
    "status",
    [ContactStatus.PENDING, ContactStatus.ACTIVE, ContactStatus.PAUSED, ContactStatus.IGNORED],
)
async def test_a_contact_that_is_no_longer_new_is_skipped(status: ContactStatus) -> None:
    rig = await _rig()
    contact = await _seed(rig, 1, status=status, profile=COLLECTED, mode_id=1)
    async with _running(rig):
        await rig.ready.publish(ContactReadyEvent(contact_id=contact.id))
        await _until(lambda: _skipped(rig, 1))

    assert rig.sender.cards == []
    assert await _status(rig, contact) is status


async def _skipped(rig: Rig, count: int) -> bool:
    return rig.announcer.skipped == count


async def test_an_event_for_a_contact_that_does_not_exist_is_skipped() -> None:
    rig = await _rig()
    async with _running(rig):
        await rig.ready.publish(ContactReadyEvent(contact_id=424242))
        await _until(lambda: _skipped(rig, 1))

    assert rig.sender.cards == []


async def test_the_sweep_picks_new_contacts_by_the_collection_and_age_rules() -> None:
    rig = await _rig()
    collected_recent = await _seed(rig, 1, profile=COLLECTED, created_at=RECENT)
    uncollected_old = await _seed(rig, 2, created_at=OLD)
    uncollected_recent = await _seed(rig, 3, created_at=RECENT)
    collected_active = await _seed(
        rig, 4, status=ContactStatus.ACTIVE, profile=COLLECTED, mode_id=1
    )
    broken_profile_recent = await _seed(rig, 5, profile={"photo": 5}, created_at=RECENT)
    broken_profile_old = await _seed(rig, 6, profile={"photo": 5}, created_at=OLD)
    async with _running(rig):
        await _until(lambda: _is(rig, broken_profile_old, ContactStatus.PENDING))
        await _settled(rig)

    announced = {c.contact_id for c in rig.sender.cards}
    assert announced == {collected_recent.id, uncollected_old.id, broken_profile_old.id}
    assert await _status(rig, uncollected_recent) is ContactStatus.NEW
    assert await _status(rig, broken_profile_recent) is ContactStatus.NEW
    assert await _status(rig, collected_active) is ContactStatus.ACTIVE


async def test_a_contact_one_second_short_of_the_delay_waits() -> None:
    rig = await _rig()
    waiting = await _seed(rig, 1, created_at=OLD + timedelta(seconds=1))
    async with _running(rig):
        await _settled(rig)

    assert rig.sender.cards == []
    assert await _status(rig, waiting) is ContactStatus.NEW


async def test_the_startup_sweep_announces_contacts_without_any_event() -> None:
    rig = await _rig()
    first = await _seed(rig, 1, profile=COLLECTED)
    second = await _seed(rig, 2, profile=COLLECTED)
    async with _running(rig):
        await _until(lambda: _is(rig, second, ContactStatus.PENDING))

    assert [c.contact_id for c in rig.sender.cards] == [first.id, second.id]


async def test_a_contact_that_appears_later_is_found_by_a_later_sweep() -> None:
    rig = await _rig(sweep_interval=SHORT_INTERVAL)
    async with _running(rig):
        await _settled(rig)
        late = await _seed(rig, 1, profile=COLLECTED)
        await _until(lambda: _is(rig, late, ContactStatus.PENDING))


async def test_the_same_contact_announced_by_an_event_and_a_sweep_is_never_in_a_wrong_status() -> (
    None
):
    rig = await _rig(sender=FakeCardSender(delay=0.05), sweep_interval=SHORT_INTERVAL)
    contact = await _seed(rig, 1, profile=COLLECTED)
    async with _running(rig):
        await rig.ready.publish(ContactReadyEvent(contact_id=contact.id))
        await rig.ready.publish(ContactReadyEvent(contact_id=contact.id))
        await _until(lambda: _is(rig, contact, ContactStatus.PENDING))
        await _settled(rig)

    assert 1 <= len(rig.sender.cards) <= 2
    assert rig.contacts.status_changes == [(contact.id, ContactStatus.PENDING)]
    assert await _status(rig, contact) is ContactStatus.PENDING


async def test_contacts_are_announced_strictly_one_at_a_time() -> None:
    rig = await _rig(sender=FakeCardSender(delay=0.01))
    contacts = [await _seed(rig, tg_id, profile=COLLECTED) for tg_id in range(1, 7)]
    async with _running(rig):
        for contact in contacts:
            await rig.ready.publish(ContactReadyEvent(contact_id=contact.id))
        await _until(lambda: _is(rig, contacts[-1], ContactStatus.PENDING))

    assert rig.sender.max_active == 1
    assert [c.contact_id for c in rig.sender.cards] == [c.id for c in contacts]


async def test_cancellation_is_clean_and_leaves_no_tasks_behind() -> None:
    rig = await _rig(sender=FakeCardSender(delay=3600))
    await _seed(rig, 1, profile=COLLECTED)
    before = asyncio.all_tasks()
    task = asyncio.create_task(rig.announcer.run())
    await _until(lambda: bool(rig.sender.cards))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert asyncio.all_tasks() == before


async def test_a_failing_sweep_is_counted_and_the_next_sweep_still_runs() -> None:
    class _FlakyContacts(InMemoryContacts):
        failures = 1

        async def list_by_status(self, status: ContactStatus) -> list[Contact]:
            if self.failures:
                self.failures -= 1
                raise ConnectionError("fake database failure")
            return await super().list_by_status(status)

    rig = await _rig(contacts=_FlakyContacts(), sweep_interval=SHORT_INTERVAL)
    contact = await _seed(rig, 1, profile=COLLECTED)
    async with _running(rig):
        await _until(lambda: _is(rig, contact, ContactStatus.PENDING))

    assert rig.announcer.failed == 1


async def test_repeated_failures_in_one_sweep_log_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = await _rig(sender=FakeCardSender(results=[False] * 5))
    for tg_id in range(1, 6):
        await _seed(
            rig, tg_id, profile=COLLECTED, username="secret_nick", display_name="Secret Name"
        )
    with caplog.at_level(logging.INFO, logger="cyrano.services.card_announcer"):
        async with _running(rig):
            await _until(lambda: _deferred_at_least(rig, 5))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    logged = "".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for private in ("secret_nick", "Secret Name", "fake bio"):
        assert private not in logged


async def _deferred_at_least(rig: Rig, count: int) -> bool:
    return rig.announcer.deferred >= count


async def test_a_new_sweep_allows_the_next_warning(caplog: pytest.LogCaptureFixture) -> None:
    rig = await _rig(sender=FakeCardSender(results=[False] * 50), sweep_interval=SHORT_INTERVAL)
    await _seed(rig, 1, profile=COLLECTED)
    with caplog.at_level(logging.WARNING, logger="cyrano.services.card_announcer"):
        async with _running(rig):
            await _until(lambda: _deferred_at_least(rig, 4))

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) >= 2


async def test_a_successful_announcement_logs_the_internal_id_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = await _rig()
    contact = await _seed(rig, 1, profile=COLLECTED, username="secret_nick", display_name="Secret")
    with caplog.at_level(logging.INFO):
        async with _running(rig):
            await _until(lambda: _is(rig, contact, ContactStatus.PENDING))

    [record] = [r for r in caplog.records if r.getMessage() == "card announced"]
    assert record.__dict__["contact_id"] == contact.id
    logged = "".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "secret_nick" not in logged
    assert "Secret" not in logged


def test_the_named_constants_have_the_agreed_values() -> None:
    assert CARD_SWEEP_INTERVAL == 120.0
    assert CARD_WITHOUT_PROFILE_DELAY == 600.0
