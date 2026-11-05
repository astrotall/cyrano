from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta

import pytest
from sqlalchemy.engine import URL

from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlScheduleProfileRepository,
)
from cyrano.domain.enums import BlockKind, ContactStatus, MessageDirection, PlanReason
from cyrano.domain.errors import NoMessagesToReplyTo, PlanNotPossible
from cyrano.domain.models import Contact, MessageCreate
from cyrano.domain.schedule import default_schedule_profile
from cyrano.services.humanization import (
    PLANNER_HISTORY_MESSAGES,
    HumanizationPlanner,
    ScheduleService,
)
from tests.db.conftest import execute, fetch_all
from tests.privacy import FAKE_INTERNAL_ID, FAKE_OTHER_INTERNAL_ID, FAKE_TELEGRAM_ID, assert_absent
from tests.services.outbox_fakes import stored_contact
from tests.services.schedule_data import MOSCOW, TUESDAY, at_local, profile_with

TEXT = "planner-text-marker-zq"
DISPLAY_NAME = "PlannerNameMarkerZq"
USERNAME = "planner_username_marker_zq"
NOON = at_local(TUESDAY, 12)
SEED = 17
INCOMING = MessageDirection.INCOMING
OUTGOING = MessageDirection.OUTGOING
ONGOING_RANGE = (1, 4)
NEW_RANGE = (20, 120)


@pytest.fixture
async def contact(contacts: SqlContactRepository, modes: SqlModeRepository) -> Contact:
    return await stored_contact(
        contacts,
        modes,
        ContactStatus.ACTIVE,
        tg_id=FAKE_TELEGRAM_ID,
        username=USERNAME,
        display_name=DISPLAY_NAME,
    )


@pytest.fixture
def planner(
    messages: SqlMessageRepository, schedule_profiles: SqlScheduleProfileRepository
) -> HumanizationPlanner:
    return HumanizationPlanner(
        messages, ScheduleService(schedule_profiles, MOSCOW), random.Random(SEED)
    )


async def _store(
    messages: SqlMessageRepository,
    url: URL,
    contact: Contact,
    *history: tuple[MessageDirection, datetime],
) -> list[int]:
    ids: list[int] = []
    for number, (direction, at) in enumerate(history, start=1):
        message = await messages.append(
            MessageCreate(
                contact_id=contact.id,
                direction=direction,
                text=TEXT,
                tg_message_id=800_000 + number,
            )
        )
        await execute(
            url,
            f"UPDATE messages SET created_at = '{at.isoformat()}' "  # noqa: S608
            f"WHERE id = {message.id}",
        )
        ids.append(message.id)
    return ids


def _seconds(plan_at: datetime, origin: datetime) -> float:
    return (plan_at - origin).total_seconds()


async def test_a_burst_after_an_outgoing_message_inside_the_window_is_ongoing(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    ids = await _store(
        messages,
        migrated_url,
        contact,
        (OUTGOING, NOON - timedelta(minutes=5)),
        (INCOMING, NOON - timedelta(minutes=1)),
        (INCOMING, NOON - timedelta(seconds=40)),
        (INCOMING, NOON),
    )

    plan = await planner.plan(contact.id, ids[-1], NOON)

    assert plan.reason is PlanReason.ONGOING
    assert ONGOING_RANGE[0] <= _seconds(plan.send_at, NOON) <= ONGOING_RANGE[1]


async def test_several_incoming_with_a_long_gap_before_them_are_a_new_conversation(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    ids = await _store(
        messages,
        migrated_url,
        contact,
        (OUTGOING, NOON - timedelta(hours=3)),
        (INCOMING, NOON - timedelta(seconds=30)),
        (INCOMING, NOON),
    )

    plan = await planner.plan(contact.id, ids[-1], NOON)

    assert plan.reason is PlanReason.DELAYED
    assert NEW_RANGE[0] <= _seconds(plan.send_at, NOON) <= NEW_RANGE[1]


async def test_ongoing_is_measured_from_the_first_message_of_the_burst_not_the_trigger(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    ids = await _store(
        messages,
        migrated_url,
        contact,
        (OUTGOING, NOON - timedelta(minutes=30)),
        (INCOMING, NOON - timedelta(minutes=25)),
        (INCOMING, NOON),
    )

    assert (await planner.plan(contact.id, ids[-1], NOON)).reason is PlanReason.ONGOING


async def test_a_gap_of_the_whole_window_before_the_burst_means_new(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    ids = await _store(
        messages,
        migrated_url,
        contact,
        (OUTGOING, NOON - timedelta(minutes=12)),
        (INCOMING, NOON - timedelta(minutes=2)),
        (INCOMING, NOON),
    )

    assert (await planner.plan(contact.id, ids[-1], NOON)).reason is PlanReason.DELAYED


async def test_no_history_before_the_burst_means_new(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    ids = await _store(
        messages,
        migrated_url,
        contact,
        (INCOMING, NOON - timedelta(seconds=10)),
        (INCOMING, NOON),
    )

    assert (await planner.plan(contact.id, ids[-1], NOON)).reason is PlanReason.DELAYED


async def test_a_single_first_message_means_new(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    ids = await _store(messages, migrated_url, contact, (INCOMING, NOON))

    assert (await planner.plan(contact.id, ids[0], NOON)).reason is PlanReason.DELAYED


async def test_messages_after_the_trigger_are_ignored(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    ids = await _store(
        messages,
        migrated_url,
        contact,
        (OUTGOING, NOON - timedelta(hours=2)),
        (INCOMING, NOON),
        (OUTGOING, NOON + timedelta(seconds=30)),
        (INCOMING, NOON + timedelta(seconds=40)),
    )

    plan = await planner.plan(contact.id, ids[1], NOON)

    assert plan.reason is PlanReason.DELAYED
    assert NEW_RANGE[0] <= _seconds(plan.send_at, NOON) <= NEW_RANGE[1]


async def test_an_older_trigger_counts_only_the_run_that_ends_with_it(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    ids = await _store(
        messages,
        migrated_url,
        contact,
        (INCOMING, NOON - timedelta(hours=5)),
        (OUTGOING, NOON - timedelta(minutes=2)),
        (INCOMING, NOON - timedelta(minutes=1)),
        (INCOMING, NOON),
    )

    assert (await planner.plan(contact.id, ids[2], NOON)).reason is PlanReason.ONGOING


async def test_previous_activity_may_be_an_incoming_message_only_when_it_ends_a_run(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    ids = await _store(
        messages,
        migrated_url,
        contact,
        (INCOMING, NOON - timedelta(hours=1)),
        (OUTGOING, NOON - timedelta(minutes=1)),
        (INCOMING, NOON),
    )

    assert (await planner.plan(contact.id, ids[-1], NOON)).reason is PlanReason.ONGOING


async def test_the_delay_is_measured_from_the_trigger_message(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    trigger_at = NOON - timedelta(seconds=10)
    ids = await _store(messages, migrated_url, contact, (INCOMING, trigger_at))
    expected = trigger_at + timedelta(seconds=random.Random(SEED).uniform(*NEW_RANGE))

    plan = await planner.plan(contact.id, ids[0], NOON)

    assert plan.send_at == max(NOON, expected)


async def test_a_trigger_that_is_not_in_the_history_raises_with_ids_only(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    await _store(messages, migrated_url, contact, (INCOMING, NOON))

    with pytest.raises(PlanNotPossible) as caught:
        await planner.plan(contact.id, FAKE_INTERNAL_ID, NOON)

    assert caught.value.contact_id == contact.id
    assert caught.value.message_id == FAKE_INTERNAL_ID
    assert_absent(TEXT, str(caught.value))
    assert_absent(FAKE_TELEGRAM_ID, str(caught.value))


async def test_a_trigger_of_another_contact_raises(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    other = await stored_contact(
        contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_OTHER_INTERNAL_ID
    )
    other_ids = await _store(messages, migrated_url, other, (INCOMING, NOON))
    await _store(messages, migrated_url, contact, (INCOMING, NOON))

    with pytest.raises(PlanNotPossible):
        await planner.plan(contact.id, other_ids[0], NOON)


async def test_an_outgoing_trigger_raises(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    ids = await _store(messages, migrated_url, contact, (INCOMING, NOON), (OUTGOING, NOON))

    with pytest.raises(PlanNotPossible):
        await planner.plan(contact.id, ids[1], NOON)


async def test_a_trigger_older_than_the_history_window_raises(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    moments = [
        (INCOMING, NOON + timedelta(seconds=index)) for index in range(PLANNER_HISTORY_MESSAGES + 1)
    ]
    ids = await _store(messages, migrated_url, contact, *moments)

    with pytest.raises(PlanNotPossible):
        await planner.plan(contact.id, ids[0], NOON)


async def test_the_plan_writes_nothing(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    ids = await _store(messages, migrated_url, contact, (INCOMING, NOON))
    query = "SELECT count(*) FROM {}"
    before = [
        await fetch_all(migrated_url, query.format(table))
        for table in ("messages", "scheduled_messages", "schedule_profile")
    ]

    await planner.plan(contact.id, ids[0], NOON)

    after = [
        await fetch_all(migrated_url, query.format(table))
        for table in ("messages", "scheduled_messages", "schedule_profile")
    ]
    assert before == after


async def test_the_stored_profile_is_used_when_there_is_one(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    schedule_profiles: SqlScheduleProfileRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    await schedule_profiles.replace(
        profile_with(busy=[{"days": ["tue"], "from": "11:00", "to": "13:00"}])
    )
    ids = await _store(messages, migrated_url, contact, (INCOMING, NOON))

    plan = await planner.plan(contact.id, ids[0], NOON)

    assert plan.reason is PlanReason.POSTPONED_BUSY
    assert plan.send_at > at_local(TUESDAY, 13)


async def test_the_default_profile_is_used_when_nothing_is_stored(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    at_night = at_local(TUESDAY, 4)
    ids = await _store(messages, migrated_url, contact, (INCOMING, at_night))

    plan = await planner.plan(contact.id, ids[0], at_night)

    assert plan.reason is PlanReason.POSTPONED_NIGHT
    assert plan.send_at > at_local(TUESDAY, 9)


async def test_plan_latest_takes_the_last_incoming_message(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    await _store(
        messages,
        migrated_url,
        contact,
        (OUTGOING, NOON - timedelta(hours=2)),
        (INCOMING, NOON - timedelta(hours=1)),
        (OUTGOING, NOON + timedelta(seconds=1)),
    )

    plan = await planner.plan_latest(contact.id, NOON)

    assert plan.send_at == NOON
    assert plan.reason is PlanReason.DELAYED


async def test_plan_latest_without_an_incoming_message_raises(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    await _store(messages, migrated_url, contact, (OUTGOING, NOON))

    with pytest.raises(NoMessagesToReplyTo):
        await planner.plan_latest(contact.id, NOON)


async def test_the_planner_logs_nothing_personal(
    planner: HumanizationPlanner,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    ids = await _store(messages, migrated_url, contact, (INCOMING, NOON))

    await planner.plan(contact.id, ids[0], NOON)

    for marker in (TEXT, DISPLAY_NAME, USERNAME, FAKE_TELEGRAM_ID):
        assert_absent(marker, caplog.text)


async def test_the_service_returns_the_default_profile_with_the_settings_timezone(
    schedule_profiles: SqlScheduleProfileRepository,
) -> None:
    service = ScheduleService(schedule_profiles, "Asia/Tokyo")

    assert await service.get_profile() == default_schedule_profile("Asia/Tokyo")


async def test_the_service_keeps_the_stored_timezone_and_fills_a_missing_one(
    schedule_profiles: SqlScheduleProfileRepository,
) -> None:
    service = ScheduleService(schedule_profiles, "Asia/Tokyo")

    await schedule_profiles.replace(profile_with(timezone=None))
    assert (await service.get_profile()).timezone == "Asia/Tokyo"

    await schedule_profiles.replace(profile_with(timezone="Europe/Berlin"))
    assert (await service.get_profile()).timezone == "Europe/Berlin"


async def test_blocked_until_reports_the_end_and_the_kind(
    schedule_profiles: SqlScheduleProfileRepository,
) -> None:
    service = ScheduleService(schedule_profiles, MOSCOW)

    block = await service.blocked_until(at_local(TUESDAY, 4))
    assert block is not None
    assert (block.end, block.kind) == (at_local(TUESDAY, 9), BlockKind.NIGHT)
    assert await service.blocked_until(NOON) is None
