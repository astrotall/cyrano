from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy.engine import URL

from cyrano.db.repositories import SqlSenderStateRepository
from cyrano.db.session import SessionFactory
from cyrano.domain.errors import SenderStateMissing
from cyrano.domain.outbox import SenderState
from tests.db.conftest import execute, fetch_all
from tests.services.outbox_fakes import FAKE_NOW

SOON = FAKE_NOW + timedelta(seconds=30)
LATER = FAKE_NOW + timedelta(seconds=300)
RACERS = 6


async def test_a_fresh_database_has_a_running_sender(
    sender_state: SqlSenderStateRepository,
) -> None:
    state = await sender_state.get()

    assert (state.paused_until, state.pause_reason, state.stopped, state.stop_reason) == (
        None,
        None,
        False,
        None,
    )


async def test_a_first_pause_is_new_and_is_stored(sender_state: SqlSenderStateRepository) -> None:
    assert await sender_state.pause_until(SOON, "FakeFloodWait", now=FAKE_NOW) is True

    state = await sender_state.get()
    assert (state.paused_until, state.pause_reason) == (SOON, "FakeFloodWait")


async def test_a_shorter_pause_never_shortens_a_longer_one(
    sender_state: SqlSenderStateRepository,
) -> None:
    await sender_state.pause_until(LATER, "FakeLongWait", now=FAKE_NOW)

    assert await sender_state.pause_until(SOON, "FakeShortWait", now=FAKE_NOW) is False

    state = await sender_state.get()
    assert (state.paused_until, state.pause_reason) == (LATER, "FakeLongWait")


async def test_a_longer_pause_extends_an_active_one_and_is_not_new(
    sender_state: SqlSenderStateRepository,
) -> None:
    await sender_state.pause_until(SOON, "FakeShortWait", now=FAKE_NOW)

    assert await sender_state.pause_until(LATER, "FakeLongWait", now=FAKE_NOW) is False

    state = await sender_state.get()
    assert (state.paused_until, state.pause_reason) == (LATER, "FakeLongWait")


@pytest.mark.parametrize("seconds_after_the_end", [0, 1])
async def test_a_pause_after_the_last_one_ended_is_new(
    sender_state: SqlSenderStateRepository, seconds_after_the_end: int
) -> None:
    await sender_state.pause_until(SOON, "FakeFloodWait", now=FAKE_NOW)
    now = SOON + timedelta(seconds=seconds_after_the_end)

    assert await sender_state.pause_until(now + timedelta(seconds=9), "FakeFloodWait", now=now)


async def test_a_pause_refuses_a_naive_time(sender_state: SqlSenderStateRepository) -> None:
    naive = datetime(2025, 1, 1)

    with pytest.raises(ValueError, match="timezone-aware"):
        await sender_state.pause_until(naive, "FakeFloodWait", now=FAKE_NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        await sender_state.pause_until(SOON, "FakeFloodWait", now=naive)


async def test_a_pause_ends_exactly_at_its_time(sender_state: SqlSenderStateRepository) -> None:
    await sender_state.pause_until(SOON, "FakeFloodWait", now=FAKE_NOW)

    state = await sender_state.get()
    assert state.is_paused(SOON - timedelta(seconds=1)) is True
    assert state.is_paused(SOON) is False


async def test_stop_says_whether_it_is_new_and_keeps_the_first_reason(
    sender_state: SqlSenderStateRepository,
) -> None:
    assert await sender_state.stop("FakeFirstKind") is True
    assert await sender_state.stop("FakeSecondKind") is False

    state = await sender_state.get()
    assert (state.stopped, state.stop_reason) == (True, "FakeFirstKind")


async def test_concurrent_stops_report_exactly_one_new_stop(
    session_factory: SessionFactory,
) -> None:
    repositories = [SqlSenderStateRepository(session_factory) for _ in range(RACERS)]

    results = await asyncio.gather(*(repo.stop("FakeKind") for repo in repositories))

    assert sorted(results) == [False] * (RACERS - 1) + [True]


async def test_concurrent_pauses_report_exactly_one_new_pause_and_keep_the_longest(
    session_factory: SessionFactory,
) -> None:
    repositories = [SqlSenderStateRepository(session_factory) for _ in range(RACERS)]

    results = await asyncio.gather(
        *(
            repo.pause_until(SOON + timedelta(seconds=number), "FakeFloodWait", now=FAKE_NOW)
            for number, repo in enumerate(repositories)
        )
    )

    assert sorted(results) == [False] * (RACERS - 1) + [True]
    assert (await repositories[0].get()).paused_until == SOON + timedelta(seconds=RACERS - 1)


async def test_resume_clears_both_and_returns_what_was_there(
    sender_state: SqlSenderStateRepository,
) -> None:
    await sender_state.pause_until(SOON, "FakeFloodWait", now=FAKE_NOW)
    await sender_state.stop("FakeKind")

    before = await sender_state.resume()

    assert (before.paused_until, before.pause_reason, before.stopped, before.stop_reason) == (
        SOON,
        "FakeFloodWait",
        True,
        "FakeKind",
    )
    state = await sender_state.get()
    assert (state.paused_until, state.pause_reason, state.stopped, state.stop_reason) == (
        None,
        None,
        False,
        None,
    )


async def test_resume_keeping_the_stop_changes_nothing_on_a_stopped_sender(
    sender_state: SqlSenderStateRepository,
) -> None:
    await sender_state.pause_until(SOON, "FakeFloodWait", now=FAKE_NOW)
    await sender_state.stop("FakeKind")
    stored = await sender_state.get()

    before = await sender_state.resume(keep_stop=True)

    assert before == stored
    assert await sender_state.get() == stored


async def test_resume_keeping_the_stop_clears_a_plain_pause(
    sender_state: SqlSenderStateRepository,
) -> None:
    await sender_state.pause_until(SOON, "FakeFloodWait", now=FAKE_NOW)

    await sender_state.resume(keep_stop=True)

    assert (await sender_state.get()).paused_until is None


async def test_resume_on_a_running_sender_is_harmless(
    sender_state: SqlSenderStateRepository,
) -> None:
    before = await sender_state.resume()

    assert before.stopped is False
    assert before.paused_until is None


async def test_the_state_survives_a_new_repository(
    sender_state: SqlSenderStateRepository, session_factory: SessionFactory
) -> None:
    await sender_state.stop("FakeKind")
    await sender_state.pause_until(SOON, "FakeFloodWait", now=FAKE_NOW)

    state = await SqlSenderStateRepository(session_factory).get()

    assert (state.stopped, state.paused_until) == (True, SOON)


async def test_there_is_always_one_row(
    sender_state: SqlSenderStateRepository, migrated_url: URL
) -> None:
    await sender_state.stop("FakeKind")
    await sender_state.resume()
    await sender_state.pause_until(SOON, "FakeFloodWait", now=FAKE_NOW)

    assert await fetch_all(migrated_url, "SELECT id FROM sender_state") == [(1,)]


async def test_a_missing_row_is_a_readable_domain_error_for_every_method(
    sender_state: SqlSenderStateRepository, migrated_url: URL
) -> None:
    await execute(migrated_url, "DELETE FROM sender_state")

    with pytest.raises(SenderStateMissing):
        await sender_state.get()
    with pytest.raises(SenderStateMissing):
        await sender_state.pause_until(SOON, "FakeFloodWait", now=FAKE_NOW)
    with pytest.raises(SenderStateMissing):
        await sender_state.stop("FakeKind")
    with pytest.raises(SenderStateMissing):
        await sender_state.resume()


def test_the_state_model_rejects_a_naive_time() -> None:
    with pytest.raises(ValueError):
        SenderState(
            paused_until=datetime(2025, 1, 1),
            pause_reason=None,
            stopped=False,
            stop_reason=None,
            updated_at=FAKE_NOW,
        )
